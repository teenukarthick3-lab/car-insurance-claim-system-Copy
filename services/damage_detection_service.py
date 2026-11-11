"""AI damage analysis for submitted claims.

    stored photo (read-only)  →  vision provider (same Gemini model as the photo check)
      →  damage areas with boxes, type, severity, confidence
      →  estimation_service (configurable rates)  →  saved in damage_assessments + claim_estimations

The original photo files are only ever READ. Overlays are drawn in the browser on top of the
untouched image, so the assessor can always compare the AI result with the original evidence.
Analysis runs in a background thread after submission so the customer never waits for it.
"""
import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import config
from database import models
from services import estimation_service, vision_providers
from services.vehicle_image_validator import _jpeg_for_model, _open_image
from services.vision_providers import VisionProviderError, normalize_damage_report

log = logging.getLogger(__name__)

# Photos that are analyzed for damage, with the hint passed to the model.
ANALYZED_SLOTS = {
    "front_photo": "front of the vehicle (close up)",
    "back_photo": "back of the vehicle (from a few metres away)",
    "left_photo": "vehicle's left side",
    "right_photo": "vehicle's right side",
    "rear_photo": "rear of the vehicle (previous)",
}
PENDING_TIMEOUT = timedelta(minutes=10)    # a PENDING analysis older than this was interrupted

PART_LABELS = {p: p.replace("_", " ").title().replace("Tyre", "tyre") for p in vision_providers.VEHICLE_PARTS}
PART_LABELS.update({"BOOT_TAILGATE": "Boot / tailgate", "WHEEL_TYRE": "Wheel / tyre", "OTHER": "Other"})


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def availability():
    """(ready, reason). Never raises."""
    if config.DAMAGE_ANALYSIS_MODE == "off":
        return False, "AI damage analysis is turned off (DAMAGE_ANALYSIS_MODE=off)"
    try:
        provider = vision_providers.get_provider()
        ready, why = provider.status()
    except VisionProviderError:
        log.exception("AI damage-analysis provider is unavailable")
        return False, (
            "Damage assessment is temporarily unavailable. "
            "Our claims team will review your submitted photos."
        )
    return (True, why) if ready else (False, why)


# ---------------------------------------------------------------------------
# Single image
# ---------------------------------------------------------------------------
_cache = OrderedDict()
_cache_lock = threading.Lock()


def analyze_image(data, view_hint="", force_refresh=False):
    """Detect damage in one image (bytes). Returns a normalized report. Raises VisionProviderError."""
    key = hashlib.sha256(data + view_hint.encode()).hexdigest()
    with _cache_lock:
        if not force_refresh and key in _cache:
            return json.loads(_cache[key])
    try:
        image = _open_image(data)
    except ValueError as exc:
        raise VisionProviderError("photo can't be read") from exc
    provider = vision_providers.get_provider()
    report = normalize_damage_report(provider.detect_damage(_jpeg_for_model(image), view_hint=view_hint))
    with _cache_lock:
        _cache[key] = json.dumps(report)
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return report


def clear_cache():
    with _cache_lock:
        _cache.clear()


DAMAGE_TYPE_MAP = {
    "DENT": "Dent",
    "SCRATCH": "Scratch",
    "CRACK": "Crack",
    "PAINT_DAMAGE": "Paint damage",
    "BUMPER_DAMAGE": "Bumper damage",
    "GLASS_DAMAGE": "Glass damage",
    "LAMP_DAMAGE": "Lamp damage",
    "PANEL_DEFORMATION": "Panel deformation",
    "BROKEN_OR_MISSING_PART": "Broken or missing part",
    "MIRROR_DAMAGE": "Mirror damage",
    "TYRE_OR_WHEEL_DAMAGE": "Tyre / wheel damage",
    "OTHER": "Other",
}


def _sanitize_error(error_str):
    s = str(error_str or "").strip()
    if not s:
        return "Analysis failed"
    s_low = s.lower()
    if "api key" in s_low or "rejected" in s_low or "unauthorized" in s_low or "permission" in s_low:
        return "AI service configuration issue"
    if "rate limit" in s_low or "quota" in s_low or "429" in s_low:
        return "AI service temporarily busy (rate limit)"
    if "connection" in s_low or "timeout" in s_low or "network" in s_low:
        return "Network connection to AI service failed"
    return s[:120]


def classify_damage_photo(data):
    """Analyze one damage photo using vision model to classify damage type. Never blocks/crashes."""
    ready, why = availability()
    if not ready:
        return {
            "damage_type": "Other/uncertain",
            "confidence": None,
            "status": "UNAVAILABLE",
            "message": ""
        }
    try:
        report = analyze_image(data, "close-up of vehicle damage")
        damages = report.get("damages") or []
        if not damages:
            return {
                "damage_type": "Other/uncertain",
                "confidence": None,
                "status": "MANUAL_REVIEW",
                "message": report.get("notes") or "No distinct damage detected (manual review required)"
            }
        best = max(damages, key=lambda d: d.get("confidence") or 0.0)
        raw_type = str(best.get("damage_type") or "").upper()
        mapped = DAMAGE_TYPE_MAP.get(raw_type, raw_type.replace("_", " ").title())
        conf = best.get("confidence")
        status = "COMPLETE" if conf and conf >= config.DAMAGE_CONFIDENCE_THRESHOLD else "MANUAL_REVIEW"
        return {
            "damage_type": mapped,
            "confidence": conf,
            "status": status,
            "message": best.get("reason") or "Damage detected"
        }
    except Exception:
        log.exception("Damage photo classification failed")
        return {
            "damage_type": "Other/uncertain",
            "confidence": None,
            "status": "UNAVAILABLE",
            "message": (
                "Damage assessment is temporarily unavailable. "
                "Our claims team will review your submitted photos."
            )
        }

# ---------------------------------------------------------------------------
# Whole claim
# ---------------------------------------------------------------------------
def _set_status(conn, claim_id, status, message=None, **values):
    now = _now()
    cols = {"analysis_status": status, "analysis_message": message, "updated_at": now, **values}
    existing = conn.execute("SELECT id FROM claim_estimations WHERE claim_id=?", (claim_id,)).fetchone()
    if existing:
        sets = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE claim_estimations SET {sets} WHERE claim_id=?", (*cols.values(), claim_id))
    else:
        cols["claim_id"] = claim_id
        cols["created_at"] = now
        names = ", ".join(cols)
        conn.execute(f"INSERT INTO claim_estimations ({names}) VALUES ({', '.join('?' * len(cols))})",
                     tuple(cols.values()))


def mark_pending(db_path, claim_id):
    conn = models.connect(db_path)
    try:
        _set_status(conn, claim_id, "PENDING", "AI damage analysis in progress")
    finally:
        conn.close()


def run_claim_analysis(db_path, upload_root, claim_id, force_refresh=False):
    """Analyze every exterior photo and damage photo of a claim and store damage areas + the repair estimate."""
    conn = models.connect(db_path)
    try:
        ready, why = availability()
        if not ready:
            _set_status(conn, claim_id, "UNAVAILABLE", why)
            return "UNAVAILABLE"
        _set_status(conn, claim_id, "PENDING", "AI damage analysis in progress")
        files = [dict(r) for r in conn.execute(
            f"SELECT id, file_type, file_path FROM claim_files WHERE claim_id=? AND file_type IN "
            f"({','.join('?' * len(ANALYZED_SLOTS))})", (claim_id, *ANALYZED_SLOTS))]

        # Also collect and analyze all damage photos
        dmg_photo_rows = [dict(r) for r in conn.execute(
            "SELECT id, file_path, original_filename, mime_type, file_size, uploaded_at "
            "FROM claim_damage_photos WHERE claim_id=? ORDER BY id", (claim_id,))]

        for dp in dmg_photo_rows:
            cf = conn.execute(
                "SELECT id, file_type, file_path FROM claim_files WHERE claim_id=? AND file_path=?",
                (claim_id, dp["file_path"])
            ).fetchone()
            if cf:
                files.append(dict(cf))
            else:
                cur = conn.execute(
                    "INSERT INTO claim_files (claim_id, file_type, file_path, original_filename, mime_type, file_size, uploaded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (claim_id, f"damage_photo_{dp['id']}", dp["file_path"], dp.get("original_filename"),
                     dp.get("mime_type"), dp.get("file_size"), dp.get("uploaded_at") or _now())
                )
                files.append({
                    "id": cur.lastrowid,
                    "file_type": f"damage_photo_{dp['id']}",
                    "file_path": dp["file_path"],
                })

        root = os.path.realpath(upload_root)

        def work(f):
            path = os.path.realpath(os.path.join(root, f["file_path"]))
            if not path.startswith(root + os.sep):
                raise VisionProviderError("invalid file path")
            hint = ANALYZED_SLOTS.get(f["file_type"], "close-up of vehicle damage")
            with open(path, "rb") as fh:                       # read-only: the evidence is never changed
                return analyze_image(fh.read(), hint, force_refresh=force_refresh)

        results, photo_results = {}, {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {f["id"]: (f, pool.submit(work, f)) for f in files}
            for fid, (f, fut) in futures.items():
                try:
                    results[fid] = (f, fut.result())
                    rep = results[fid][1]
                    photo_results[str(fid)] = {
                        "status": "INCONSISTENT" if rep.get("inconsistent") else "ANALYZED",
                        "notes": rep.get("notes", ""),
                        "assessable": rep.get("image_assessable", True),
                        "count": len(rep.get("damages", [])),
                        "inconsistent": rep.get("inconsistent", False),
                        "notes_has_damage": rep.get("notes_has_damage", False),
                    }
                except Exception as exc:                       # one bad photo shouldn't sink the rest
                    log.warning("Damage analysis failed for file %s: %s", fid, exc)
                    photo_results[str(fid)] = {"status": "FAILED", "error": _sanitize_error(exc)}

        if not results:
            reason = next((p.get("error") for p in photo_results.values() if p.get("error")), "no photos to analyze")
            _set_status(conn, claim_id, "FAILED", f"AI damage analysis failed: {reason}",
                        photo_results=json.dumps(photo_results))
            return "FAILED"

        detections = []
        for fid, (f, report) in results.items():
            for d in report.get("damages", []):
                detections.append(dict(d, claim_file_id=fid))
        est = estimation_service.estimate(detections)
        primary_ids = {id(x) for x in estimation_service.dedupe(
            [d for d in detections if d.get("confidence") is None or d["confidence"] >= est["threshold"]]
        )}

        provider = vision_providers.get_provider()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM damage_assessments WHERE claim_id=?", (claim_id,))
        for d in detections:
            conf = d.get("confidence")
            confirmed = conf is None or conf >= est["threshold"]
            cost = estimation_service.item_cost(d["damage_type"], d["severity"]) if confirmed else None
            conn.execute(
                """INSERT INTO damage_assessments (claim_id, claim_file_id, damage_type, vehicle_part, location,
                       severity, confidence, review_status, is_primary, estimated_cost, bounding_box, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (claim_id, d["claim_file_id"], d["damage_type"], d["vehicle_part"], d["location"], d["severity"],
                 conf if conf is not None else 1.0, "CONFIRMED" if confirmed else "POSSIBLE",
                 1 if (not confirmed or id(d) in primary_ids) else 0, cost, json.dumps(d["box"]), d["reason"], _now()))
        failed = sum(1 for p in photo_results.values() if p["status"] == "FAILED")
        _set_status(conn, claim_id, "COMPLETE",
                    f"{failed} photo(s) could not be analyzed" if failed else None,
                    provider=getattr(provider, "name", ""), model=getattr(provider, "model", ""),
                    parts_cost=est["lines"]["parts"], labour_cost=est["lines"]["labour"],
                    paint_cost=est["lines"]["paint"], other_cost=est["lines"]["other"],
                    total_estimated_cost=est["total"], currency=est["currency"],
                    photo_results=json.dumps(photo_results))
        conn.commit()
        return "COMPLETE"
    except Exception as exc:
        log.exception("Damage analysis crashed for claim %s", claim_id)
        try:
            conn.rollback()
            _set_status(
                conn,
                claim_id,
                "FAILED",
                (
                    "Damage assessment is temporarily unavailable. "
                    "Our claims team will review your submitted photos."
                )
            )
        except Exception:
            pass
        return "FAILED"
    finally:
        conn.close()


def start_background_analysis(db_path, upload_root, claim_id, wait=False, force_refresh=False):
    """Kick off analysis without blocking the request. Claims without AI get an UNAVAILABLE record."""
    ready, why = availability()
    if not ready:
        conn = models.connect(db_path)
        try:
            _set_status(conn, claim_id, "UNAVAILABLE", why)
        finally:
            conn.close()
        return None
    mark_pending(db_path, claim_id)
    thread = threading.Thread(target=run_claim_analysis, args=(db_path, upload_root, claim_id),
                              kwargs={"force_refresh": force_refresh},
                              name=f"damage-analysis-{claim_id}", daemon=True)
    thread.start()
    if wait:
        thread.join()
    return thread


# ---------------------------------------------------------------------------
# Read model for the claim page
# ---------------------------------------------------------------------------
def get_claim_analysis(db, claim_id):
    """Everything the claim page needs. Works for old claims with no analysis at all."""
    row = db.execute("SELECT * FROM claim_estimations WHERE claim_id=?", (claim_id,)).fetchone()
    ready, why = availability()
    info = {"status": "NONE", "message": None, "ai_ready": ready, "ai_reason": why,
            "by_file": {}, "damage_items": [], "unpriced": [], "possible": [], "lines": None, "total": None,
            "total_min": 0, "total_max": 0, "excluded_count": 0, "by_type_breakdown": {},
            "analyzed_photos": [], "currency": config.CURRENCY_SYMBOL, "confirmed_count": 0, "photo_results": {}}
    if not row:
        return info
    row = dict(row)
    status = row["analysis_status"]
    if status == "PENDING":
        try:
            started = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - started > PENDING_TIMEOUT:
                status, row["analysis_message"] = "FAILED", "The analysis was interrupted (e.g. the server restarted)."
        except (TypeError, ValueError):
            pass
    info.update(status=status, message=row["analysis_message"], model=row.get("model"),
                analyzed_at=row["updated_at"], currency=row.get("currency") or config.CURRENCY_SYMBOL)
    try:
        info["photo_results"] = json.loads(row.get("photo_results") or "{}")
    except ValueError:
        info["photo_results"] = {}
    if status != "COMPLETE":
        return info

    info["lines"] = [(estimation_service.LINE_LABELS[k], row[f"{k}_cost"] or 0) for k in estimation_service.LINES]
    info["total"] = row["total_estimated_cost"] or 0
    breakdown = {}
    for d in db.execute("SELECT * FROM damage_assessments WHERE claim_id=? ORDER BY confidence DESC", (claim_id,)):
        d = dict(d)
        try:
            d["box"] = json.loads(d["bounding_box"])
        except ValueError:
            continue
        dtype = str(d.get("damage_type") or "").strip().upper()
        sev = str(d.get("severity") or "").strip().upper()
        label = estimation_service.TYPE_LABELS.get(dtype, dtype.replace("_", " ").title())
        d["label"] = label
        d["severity_label"] = estimation_service.SEVERITY_LABELS.get(sev, sev.title())
        d["part_label"] = PART_LABELS.get(d.get("vehicle_part") or "OTHER", "Other")
        conf = d.get("confidence")
        d["confidence_label"] = "High" if (conf is not None and conf >= 0.85) else ("Medium" if (conf is not None and conf >= config.DAMAGE_CONFIDENCE_THRESHOLD) else "Low")

        cost_rng = estimation_service.item_cost_range(dtype, sev)
        if cost_rng:
            d["cost_min"], d["cost_max"] = cost_rng
        else:
            d["cost_min"], d["cost_max"] = None, None

        info["by_file"].setdefault(d["claim_file_id"], []).append(d)
        if d["review_status"] == "POSSIBLE":
            info["possible"].append(d)
        elif d["is_primary"]:
            if d["cost_min"] is not None:
                info["damage_items"].append(d)
            else:
                info["unpriced"].append(d)
            breakdown[label] = breakdown.get(label, 0) + 1

    info["confirmed_count"] = len(info["damage_items"]) + len(info["unpriced"])
    info["excluded_count"] = len(info["unpriced"])
    info["total_min"] = sum(x["cost_min"] for x in info["damage_items"])
    info["total_max"] = sum(x["cost_max"] for x in info["damage_items"])
    info["by_type_breakdown"] = breakdown

    # List of all analyzed photos for the admin viewer tabs
    analyzed_photos = []
    view_titles = {
        "front_photo": "Front",
        "back_photo": "Back",
        "left_photo": "Left",
        "right_photo": "Right",
        "rear_photo": "Rear View (previous)",
    }
    for key in ["front_photo", "back_photo", "left_photo", "right_photo", "rear_photo"]:
        r = db.execute("SELECT id, file_type, file_path FROM claim_files WHERE claim_id=? AND file_type=?", (claim_id, key)).fetchone()
        if r:
            short = view_titles.get(key, key)
            analyzed_photos.append({"id": r["id"], "file_type": key, "title": short, "short": short, "file_path": r["file_path"]})

    for idx, r in enumerate(db.execute("SELECT id, file_path, original_filename FROM claim_damage_photos WHERE claim_id=? ORDER BY id", (claim_id,)), 1):
        cf = db.execute("SELECT id FROM claim_files WHERE claim_id=? AND file_path=?", (claim_id, r["file_path"])).fetchone()
        fid = cf["id"] if cf else r["id"]
        short = f"Damage #{idx}"
        analyzed_photos.append({"id": fid, "file_type": f"damage_photo_{r['id']}", "title": short, "short": short, "file_path": r["file_path"]})

    info["analyzed_photos"] = analyzed_photos
    return info

