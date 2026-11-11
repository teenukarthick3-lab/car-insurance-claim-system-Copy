"""Chassis / VIN number extraction from the customer's chassis-plate photo.

Flow:   photo (read-only) → text reading → candidate chassis numbers → validation → status

Text reading reuses the project's existing vision model (the photo check asks Gemini to
transcribe the plate, so there is no extra AI call). If no AI is configured, an optional local
Tesseract install is used when available (no Python package needed). The deterministic parser
below picks the chassis number and keeps it apart from the engine number.

Statuses
  VERIFIED          valid 17-character VIN, no corrections, and independent reads agree
  MANUAL_REVIEW     a number was found but it is uncertain → claims team must confirm/edit
  NOT_DETECTED      no chassis number could be read
  MANUALLY_VERIFIED entered/confirmed by a claims officer
"""
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
from collections import Counter

from PIL import ImageFilter, ImageOps

import config


import re

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")  # 17 chars, no I, O, Q


def normalize_chassis(raw):
    """Uppercase; drop spaces, hyphens, dots and the * start/end marks."""
    if not raw:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper())


def check_chassis(raw):
    """Returns dict:
       value      -> valid 17-char VIN, or None
       candidate  -> normalized text (may be invalid), for 'unverified' display
       suggestion -> 17-char VIN found by dropping ONE stray edge character
                     (plate start/end mark misread as X/I/1). Never auto-accepted;
                     the claims officer must confirm it."""
    cand = normalize_chassis(raw)
    if _VIN_RE.match(cand):
        return {"value": cand, "candidate": cand, "suggestion": None}
    suggestion = None
    if len(cand) == 18:
        for trimmed in (cand[1:], cand[:-1]):
            edge = cand[0] if trimmed == cand[1:] else cand[-1]
            if edge in "XI1" and _VIN_RE.match(trimmed):
                suggestion = trimmed
                break
    return {"value": None, "candidate": cand or None, "suggestion": suggestion}

log = logging.getLogger(__name__)

VERIFIED, MANUAL_REVIEW, NOT_DETECTED, MANUALLY_VERIFIED = "VERIFIED", "MANUAL_REVIEW", "NOT_DETECTED", "MANUALLY_VERIFIED"

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")            # ISO 3779: 17 chars, never I, O or Q
CHASSIS_LABELS = re.compile(r"\b(CHASS?IS|CHASIS|CH\.?\s*NO|VIN|V\.I\.N|FRAME(\s*NO)?|VEHICLE\s+IDENTIFICATION(\s+NUMBER|\s+NO)?)\b")
ENGINE_LABELS = re.compile(r"\b(ENGINE|ENG|E\s*/\s*NO|E\.?\s*NO|MOTOR(\s*NO)?|ENGINE\s*NO)\b")
NOISE_WORDS = {"MARUTI", "SUZUKI", "INDIA", "LTD", "LIMITED", "MADE", "IN", "MODEL", "TYPE", "WEIGHT", "GVW", "KG",
               "MFG", "MANUFACTURED", "MONTH", "YEAR", "PRIVATE", "CO", "MOTOR", "MOTORS", "HYUNDAI", "TATA", "HONDA",
               "MAHINDRA", "KIA", "TOYOTA", "FORD", "RENAULT", "NISSAN", "SKODA", "VOLKSWAGEN", "CARS", "NO", "NUMBER"}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def normalize_chassis(text):
    """Uppercase and remove spaces/punctuation that OCR adds. Never changes letters or digits."""
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def is_valid_vin(value):
    return bool(VIN_RE.match(value or ""))


def vin_corrections(value):
    """I/O/Q can never appear in a VIN, so OCR reading them means 1/0/0. Returns (fixed, changes)."""
    fixes = {"I": "1", "O": "0", "Q": "0"}
    out, changes = [], []
    for i, ch in enumerate(value):
        if ch in fixes:
            changes.append(f"position {i + 1}: {ch}→{fixes[ch]}")
            out.append(fixes[ch])
        else:
            out.append(ch)
    return "".join(out), changes


# ---------------------------------------------------------------------------
# Candidate extraction from OCR lines
# ---------------------------------------------------------------------------
def _clean_line(line):
    line = str(line or "").upper()
    line = line.replace("—", "-").replace("–", "-").replace("|", " ")
    return re.sub(r"\s+", " ", line).strip()


def find_candidates(lines):
    """Score every plausible chassis-number string in OCR lines. Returns sorted list of dicts."""
    lines = [_clean_line(l) for l in (lines or []) if str(l or "").strip()]
    cands = {}
    for idx, line in enumerate(lines):
        prev = lines[idx - 1] if idx else ""
        engine_ctx = bool(ENGINE_LABELS.search(line)) or (bool(ENGINE_LABELS.search(prev)) and len(normalize_chassis(prev)) < 12)
        chassis_ctx = bool(CHASSIS_LABELS.search(line)) or (bool(CHASSIS_LABELS.search(prev)) and len(normalize_chassis(prev)) < 14)
        # Drop the label words so "CHASSIS NO. MA3..." yields only the number.
        body = CHASSIS_LABELS.sub(" ", ENGINE_LABELS.sub(" ", line))
        # A "?" / "*" / "_" means a character the reader could not make out: never drop it silently.
        tokens = [normalize_chassis(t) if not re.search(r"[?*_]", t) else "" for t in re.split(r"[\s:;,]+", body)]
        tokens = [t for t in tokens if t and t not in NOISE_WORDS]
        # Single tokens and runs of adjacent tokens (OCR often splits a number with a space).
        for i in range(len(tokens)):
            joined = ""
            for j in range(i, min(len(tokens), i + 4)):
                joined += tokens[j]
                if len(joined) > 20:
                    break
                if len(joined) >= 10 and re.search(r"[A-Z]", joined) and re.search(r"\d", joined):
                    _score(cands, joined, line, engine_ctx, chassis_ctx, split=(j > i))
    return sorted(cands.values(), key=lambda c: -c["score"])


def _score(cands, value, line, engine_ctx, chassis_ctx, split):
    s = 0
    if len(value) == 17:
        s += 40
        if is_valid_vin(value):
            s += 30
        elif is_valid_vin(vin_corrections(value)[0]):
            s += 12
    elif 11 <= len(value) <= 20:
        s += 5
    if chassis_ctx:
        s += 30
    if engine_ctx:
        s -= 120                      # never take the engine number
    if re.search(r"\d{4,}$", value):
        s += 6                        # VIN serial part ends in digits
    if re.match(r"^[A-Z]{2}", value):
        s += 3                        # world manufacturer code starts with letters (e.g. MA = India)
    if split:
        s -= 4
    key = value
    if key not in cands or cands[key]["score"] < s:
        cands[key] = {"value": value, "score": s, "line": line, "engine_context": engine_ctx,
                      "chassis_context": chassis_ctx}


def pick_chassis(lines, model_vin=None, model_engine=None):
    """Choose the chassis number from OCR lines (+ the model's own pick when available)."""
    cands = find_candidates(lines)
    engine_norm = normalize_chassis(model_engine) if model_engine else ""
    model_norm = normalize_chassis(model_vin) if model_vin and not re.search(r"[?*_]", model_vin) else ""
    if model_norm and model_norm == engine_norm:
        model_norm = ""                               # model confused the engine number: ignore it
    if model_norm and any(c["value"] == model_norm and c["engine_context"] for c in cands):
        model_norm = ""
    usable = [c for c in cands if not c["engine_context"] and c["value"] != engine_norm and c["score"] > 0]
    if model_norm and len(model_norm) >= 10 and not any(c["value"] == model_norm for c in usable):
        usable.append({"value": model_norm, "score": 45 + (30 if is_valid_vin(model_norm) else 0),
                       "line": "(model reading)", "engine_context": False, "chassis_context": False})
        usable.sort(key=lambda c: -c["score"])
    return usable, model_norm


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def decide(reads, source, model_confidence=None, legibility=None):
    """reads: list of {"lines": [...], "model_vin": str|None, "model_engine": str|None} (one per reading).

    Returns the stored result: number, status, confidence, message, audit."""
    votes, audit_reads, engine_seen = Counter(), [], set()
    model_pick = None
    for r in reads:
        usable, model_norm = pick_chassis(r.get("lines"), r.get("model_vin"), r.get("model_engine"))
        best = usable[0]["value"] if usable else None
        if best:
            votes[best] += 1
        if model_norm:
            model_pick = model_norm
            if model_norm != best:
                votes[model_norm] += 1
        if r.get("model_engine"):
            engine_seen.add(normalize_chassis(r["model_engine"]))
        audit_reads.append({"lines": [str(l)[:120] for l in (r.get("lines") or [])][:30], "best": best,
                            "model_vin": r.get("model_vin"), "model_engine": r.get("model_engine"),
                            "candidates": [{"value": c["value"], "score": c["score"]} for c in usable[:5]]})

    audit = {"source": source, "reads": audit_reads, "engine_number": next(iter(engine_seen), None) or None}
    if not votes:
        return {"number": None, "status": NOT_DETECTED, "confidence": 0.0, "audit": audit,
                "message": "Chassis number could not be confidently detected."}

    value, count = votes.most_common(1)[0]
    total = sum(votes.values())
    corrected, changes = (value, [])
    if len(value) == 17 and not is_valid_vin(value):
        corrected, changes = vin_corrections(value)
    number = corrected if is_valid_vin(corrected) else value
    audit.update(corrections=changes, votes=dict(votes))

    if not is_valid_vin(number):
        return {"number": None, "status": NOT_DETECTED if source != "ai" else MANUAL_REVIEW,
                "confidence": 0.0, "audit": dict(audit, reason="doesn't match the 17-character VIN format"),
                "message": "Manual review required"}
    agreement = count / total
    valid = is_valid_vin(number)
    independent_agree = count >= 2
    conf = (0.45 if valid else 0.15) + 0.35 * agreement + (0.15 if independent_agree else 0) - (0.2 if changes else 0)
    if model_confidence is not None:
        conf = min(conf, 0.5 + 0.5 * model_confidence)
    if legibility == "ILLEGIBLE":
        conf = min(conf, 0.4)
    conf = round(max(0.0, min(0.99, conf)), 2)

    verified = (valid and not changes and independent_agree and agreement == 1.0 and source == "ai"
                and legibility in (None, "CLEAR") and conf >= config.CHASSIS_OCR_CONFIDENCE)
    if verified:
        return {"number": number, "status": VERIFIED, "confidence": conf, "audit": audit,
                "message": "Chassis number detected."}
    reason = ("OCR corrections were needed" if changes else
              "readings disagree" if agreement < 1 else
              "read only once" if not independent_agree else
              "read by basic OCR only" if source != "ai" else "low confidence")
    return {"number": number, "status": MANUAL_REVIEW, "confidence": conf, "audit": dict(audit, reason=reason),
            "message": "Manual review required"}


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------
def from_photo_check(check, image_bytes=None):
    """Build the chassis result from the existing photo-check result (no extra AI call).

    Falls back to local Tesseract when the AI didn't read the plate and Tesseract is installed."""
    details = (check or {}).get("details") or {}
    lines = details.get("plate_text_lines") or []
    model_vin, model_engine = details.get("vin_text"), details.get("engine_number_text")
    if lines or model_vin:
        reads = [{"lines": lines, "model_vin": None, "model_engine": model_engine}]
        if model_vin:
            reads.append({"lines": [], "model_vin": model_vin, "model_engine": model_engine})
        return decide(reads, "ai", model_confidence=check.get("confidence"), legibility=details.get("vin_legibility"))
    if image_bytes is not None and tesseract_available():
        reads = _tesseract_reads(image_bytes)
        if reads:
            return decide(reads, "tesseract")
    return {"number": None, "status": NOT_DETECTED, "confidence": 0.0,
            "audit": {"source": "none", "reason": "no OCR available (AI not configured, Tesseract not installed)"},
            "message": "Chassis number could not be confidently detected."}


def tesseract_cmd():
    configured = config.TESSERACT_CMD
    if configured and os.path.exists(configured):
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    return win if os.path.exists(win) else None


def tesseract_available():
    return config.CHASSIS_OCR_FALLBACK == "tesseract" and tesseract_cmd() is not None


_tess_cache = {}


def _tesseract_reads(image_bytes):
    """Several preprocessing variants → several independent-ish readings for voting."""
    key = hashlib.sha256(image_bytes).hexdigest()
    if key in _tess_cache:
        return json.loads(_tess_cache[key])
    from services.vehicle_image_validator import _open_image
    try:
        img = _open_image(image_bytes)
    except ValueError:
        return []
    gray = ImageOps.grayscale(img)
    gray.thumbnail((1600, 1600))                     # phone photos are large; OCR doesn't need it all
    if max(gray.size) < 1000:
        gray = gray.resize((gray.width * 2, gray.height * 2))
    jobs = [(ImageOps.autocontrast(gray), "6"),
            (ImageOps.autocontrast(gray.filter(ImageFilter.SHARPEN)), "6"),
            (ImageOps.autocontrast(gray), "11")]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        texts = list(pool.map(lambda j: _run_tesseract(*j), jobs))
    reads = [{"lines": t.splitlines(), "model_vin": None, "model_engine": None} for t in texts if t]
    _tess_cache[key] = json.dumps(reads)
    return reads


def _run_tesseract(image, psm):
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    try:
        out = subprocess.run([tesseract_cmd(), "stdin", "stdout", "--psm", psm], input=buf.getvalue(),
                             capture_output=True, timeout=config.TESSERACT_TIMEOUT,
                             env=dict(os.environ, OMP_THREAD_LIMIT="1"),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Tesseract failed: %s", exc)
        return ""


def status_label(status):
    return {VERIFIED: "Verified", MANUALLY_VERIFIED: "Verified (manual check)",
            MANUAL_REVIEW: "Pending manual review", NOT_DETECTED: "Pending manual review"}.get(status, "Not recorded")
