"""Vehicle registration number extraction from uploaded vehicle photos (front/back).

Flow: photo (read-only) -> text reading (AI / Tesseract) -> normalization -> Indian registration pattern validation.
If extraction fails or does not match the valid Indian registration format, status is MANUAL_REVIEW or NOT_DETECTED.
Original images are never modified.
"""
import io
import json
import logging
import re
from PIL import ImageFilter, ImageOps

import config
from services import policy_service
from services.chassis_ocr_service import tesseract_available, tesseract_cmd

log = logging.getLogger(__name__)

VERIFIED = "VERIFIED"
MANUAL_REVIEW = "MANUAL_REVIEW"
NOT_DETECTED = "NOT_DETECTED"


def normalize_plate_candidate(raw):
    """Normalize extracted text to uppercase alphanumeric, removing spaces/dashes."""
    return policy_service.normalize_registration(raw)


def extract_from_text_lines(lines):
    """Search lines of text for a valid Indian vehicle registration number."""
    candidates = []
    for line in lines:
        cleaned = str(line or "").strip().upper()
        if not cleaned or len(cleaned) < 5:
            continue
        # Check direct normalization
        norm = normalize_plate_candidate(cleaned)
        if 6 <= len(norm) <= 11 and policy_service.is_valid_registration(norm):
            candidates.append(norm)
            continue
        # Search tokens or regex match within the line
        matches = policy_service.REGISTRATION_RE.findall(norm)
        for m in matches:
            if isinstance(m, tuple):
                m = m[0]
            if 6 <= len(m) <= 11 and policy_service.is_valid_registration(m):
                candidates.append(m)

        # Also search spaced tokens (e.g. "TN 38 CT 5038")
        tokens = re.findall(r"[A-Z0-9]+", cleaned)
        for i in range(len(tokens)):
            for j in range(i + 1, min(len(tokens) + 1, i + 5)):
                joined = "".join(tokens[i:j])
                if 6 <= len(joined) <= 11 and policy_service.is_valid_registration(joined):
                    candidates.append(joined)

    return list(dict.fromkeys(candidates))


def _tesseract_plate_reads(image_bytes):
    """Run Tesseract with plate-friendly preprocessing variants on a temporary copy."""
    from services.vehicle_image_validator import _open_image
    try:
        img = _open_image(image_bytes)
    except Exception:
        return []

    # Work strictly on a temporary in-memory copy; original image_bytes is never modified
    gray = ImageOps.grayscale(img)
    gray.thumbnail((1600, 1600))
    if max(gray.size) < 1000:
        gray = gray.resize((gray.width * 2, gray.height * 2))

    # Crop and upscale the bumper / plate area (lower region) of the vehicle on a temporary copy
    w, h = gray.size
    bumper_crop = gray.crop((int(w * 0.05), int(h * 0.35), int(w * 0.95), int(h * 0.95)))
    if bumper_crop.width > 0 and bumper_crop.height > 0:
        bumper_upscaled = bumper_crop.resize((bumper_crop.width * 2, bumper_crop.height * 2))
    else:
        bumper_upscaled = gray

    jobs = [
        (ImageOps.autocontrast(gray), "7"),   # single text line
        (ImageOps.autocontrast(gray.filter(ImageFilter.SHARPEN)), "6"),  # uniform block
        (ImageOps.autocontrast(gray), "11"),  # sparse text
        (ImageOps.autocontrast(bumper_upscaled), "7"),  # cropped & upscaled plate region
        (ImageOps.autocontrast(bumper_upscaled.filter(ImageFilter.SHARPEN)), "6"),
    ]

    import subprocess
    cmd = tesseract_cmd()
    if not cmd:
        return []

    reads = []
    for image, psm in jobs:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        try:
            out = subprocess.run(
                [cmd, "stdin", "stdout", "--psm", psm],
                input=buf.getvalue(),
                capture_output=True,
                timeout=config.TESSERACT_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            text = out.stdout.decode("utf-8", "replace")
            if text:
                for line in text.splitlines():
                    cleaned_line = line.strip()
                    # Ignore very short text fragments (less than 5 characters)
                    if len(cleaned_line) >= 5:
                        reads.append(cleaned_line)
        except Exception as exc:
            log.warning("Plate Tesseract pass failed: %s", exc)

    return reads


def extract_registration(photo_check=None, image_bytes=None):
    """Extract and validate vehicle registration number from photo check details and/or image bytes.

    Never throws. Returns dict:
      {
        "number": str or None,
        "status": "VERIFIED" | "MANUAL_REVIEW" | "NOT_DETECTED",
        "confidence": float or None,
        "raw_text": str or None,
        "message": str
      }
    """
    try:
        details = (photo_check or {}).get("details") or {}
        # 1. Check vision AI output if present
        model_plate = details.get("license_plate_text")
        lines = list(details.get("plate_text_lines") or [])

        candidates = []
        if model_plate:
            norm = normalize_plate_candidate(model_plate)
            if 6 <= len(norm) <= 11 and policy_service.is_valid_registration(norm):
                candidates.append((norm, 0.90, norm))

        line_candidates = extract_from_text_lines(lines)
        for lc in line_candidates:
            if not any(c[0] == lc for c in candidates):
                candidates.append((lc, 0.85, lc))

        if candidates:
            best_num, conf, raw = candidates[0]
            # Valid registration number extracted
            return {
                "number": best_num,
                "status": VERIFIED if conf >= 0.85 else MANUAL_REVIEW,
                "confidence": conf,
                "raw_text": best_num,
                "message": "Vehicle registration number detected."
            }

        # 2. Offline fallback with Tesseract if available
        if image_bytes and tesseract_available():
            tess_lines = _tesseract_plate_reads(image_bytes)
            tess_candidates = extract_from_text_lines(tess_lines)
            if tess_candidates:
                num = tess_candidates[0]
                return {
                    "number": num,
                    "status": MANUAL_REVIEW,  # Basic OCR alone -> team verifies
                    "confidence": 0.65,
                    "raw_text": num,
                    "message": "Vehicle registration number detected via basic OCR (requires review)."
                }

        # 3. Not detected / uncertain / invalid / partial / empty
        return {
            "number": None,
            "status": NOT_DETECTED,
            "confidence": 0.0,
            "raw_text": None,
            "message": "OCR uncertain - manual review required"
        }
    except Exception as exc:
        log.warning("Registration extraction failed: %s", exc)
        return {
            "number": None,
            "status": NOT_DETECTED,
            "confidence": 0.0,
            "raw_text": None,
            "message": "OCR uncertain - manual review required"
        }
