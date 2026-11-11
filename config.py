"""Central settings for Car Insure.

Every value can be overridden with an environment variable or a line in a `.env`
file next to app.py (see .env.example). Change defaults here, in one place.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(path=os.path.join(BASE_DIR, ".env")):
    """Minimal .env loader (KEY=value per line) so no extra package is needed."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:          # real environment wins
                os.environ[key] = value


_load_env_file()


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# ---------------------------------------------------------------------------
# AI vehicle-photo validation
# ---------------------------------------------------------------------------

# "auto":     use the AI check when an AI provider is configured (e.g. GEMINI_API_KEY is set);
#             if none is configured, photos are accepted as NOT VERIFIED (quality checks still run).
# "required": every photo must pass the AI check; without a working AI nothing can be submitted.
# "off":      never use the AI check (photos stored as NOT VERIFIED; quality checks still run).
PHOTO_VALIDATION_MODE = os.environ.get("PHOTO_VALIDATION_MODE", "auto").strip().lower()

# Minimum model confidence (0–1) needed before a photo can be accepted.
CONFIDENCE_THRESHOLD = _float("PHOTO_CONFIDENCE_THRESHOLD", 0.80)

# Which vision provider to use (see services/vision_providers.py → PROVIDERS).
PHOTO_VALIDATION_PROVIDER = os.environ.get("PHOTO_VALIDATION_PROVIDER", "gemini").strip().lower()

# Google Gemini settings.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

# Seconds to wait for the vision model before giving up on one photo.
PHOTO_VALIDATION_TIMEOUT = _float("PHOTO_VALIDATION_TIMEOUT", 45)

# Abuse guard: max photo checks one customer session may request.
MAX_PHOTO_CHECKS_PER_SESSION = int(_float("MAX_PHOTO_CHECKS_PER_SESSION", 40))

# Local image-quality limits (applied before the AI check, no network needed).
MIN_IMAGE_SIDE_PX = int(_float("MIN_IMAGE_SIDE_PX", 480))      # shorter side
MIN_BRIGHTNESS = _float("MIN_BRIGHTNESS", 25)                   # mean luminance 0–255
MIN_SHARPNESS = _float("MIN_SHARPNESS", 6)                      # edge-variance score

# ---------------------------------------------------------------------------
# AI damage analysis + repair estimation (claims team page)
# ---------------------------------------------------------------------------

# Runs automatically after each claim is submitted when an AI provider is configured
# (the same Gemini key as the photo check). "off" disables it.
DAMAGE_ANALYSIS_MODE = os.environ.get("DAMAGE_ANALYSIS_MODE", "auto").strip().lower()

# Detections below this confidence are shown as "Possible damage — requires manual review"
# and are NOT included in the repair estimate.
DAMAGE_CONFIDENCE_THRESHOLD = _float("DAMAGE_CONFIDENCE_THRESHOLD", 0.70)

# Currency symbol for estimates. Rates themselves live in services/estimation_service.py.
CURRENCY_SYMBOL = os.environ.get("CURRENCY_SYMBOL", "₹")

# ---------------------------------------------------------------------------
# Chassis number reading (OCR) from the chassis-plate photo
# ---------------------------------------------------------------------------

# Primary reader is the configured vision AI (same Gemini call as the photo check).
# Optional offline fallback: "tesseract" uses a local Tesseract install if present; "off" disables it.
CHASSIS_OCR_FALLBACK = os.environ.get("CHASSIS_OCR_FALLBACK", "tesseract").strip().lower()
TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "").strip()   # e.g. C:\Program Files\Tesseract-OCR\tesseract.exe
TESSERACT_TIMEOUT = _float("TESSERACT_TIMEOUT", 12)            # seconds per OCR pass

# Minimum combined confidence for a chassis number to be marked VERIFIED automatically.
CHASSIS_OCR_CONFIDENCE = _float("CHASSIS_OCR_CONFIDENCE", 0.80)
