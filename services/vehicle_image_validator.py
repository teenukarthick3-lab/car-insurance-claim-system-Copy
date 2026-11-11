"""AI-based vehicle photo validation.

Pipeline for every photo:

    image bytes
      → local quality checks (opens, resolution, darkness, blank, extreme blur)   no network
      → vision provider describes the photo (services/vision_providers.py)        AI
      → detected view  = FRONT | REAR | LEFT_SIDE | RIGHT_SIDE | CHASSIS | UNKNOWN
      → compare with the view expected for the slot + confidence threshold
      → ACCEPT / REJECT, with a customer-friendly message

The decision rules live in decide() and are plain Python, so they can be unit
tested without calling any AI service. Settings are in config.py.
"""
import hashlib
import io
import logging
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

import config
from services import vision_providers
from services.vision_providers import VisionProviderError, normalize_observation

log = logging.getLogger(__name__)

# Views the classifier can report.
FRONT, REAR, LEFT_SIDE, RIGHT_SIDE, CHASSIS, UNKNOWN = "FRONT", "REAR", "LEFT_SIDE", "RIGHT_SIDE", "CHASSIS", "UNKNOWN"
VEHICLE_VIEWS = {FRONT, REAR, LEFT_SIDE, RIGHT_SIDE}

# Photo slot → the view it must show.
SLOT_EXPECTED_VIEW = {
    "front_photo": FRONT,
    "back_photo": REAR,
    "left_photo": LEFT_SIDE,
    "right_photo": RIGHT_SIDE,
    "chassis_photo": CHASSIS,
    "rear_photo": REAR,  # backward compatibility with legacy submissions
}
PHOTO_SLOTS = ("front_photo", "back_photo", "left_photo", "right_photo", "chassis_photo")

# Customer wording for each slot: (noun used in sentences, verified title)
SLOT_WORDING = {
    "front_photo": ("front view", "Front view verified"),
    "rear_photo": ("rear view", "Rear view verified"),
    "back_photo": ("back view", "Back view verified"),
    "left_photo": ("left side view", "Left side verified"),
    "right_photo": ("right side view", "Right side verified"),
    "chassis_photo": ("chassis number photo", "Chassis number detected"),
}
VIEW_WORDING = {
    REAR: ("rear view", "Rear view verified"),
    LEFT_SIDE: ("left side view", "Left side verified"),
    RIGHT_SIDE: ("right side view", "Right side verified"),
    CHASSIS: ("chassis number photo", "Chassis number detected"),
    FRONT: ("front view", "Front view verified"),
}
VIEW_PHRASE = {
    FRONT: "the front of the vehicle",
    REAR: "the rear of the vehicle",
    LEFT_SIDE: "the left side of the vehicle",
    RIGHT_SIDE: "the right side of the vehicle",
    CHASSIS: "a close-up of a chassis/VIN number",
}
SLOT_ADJECTIVE = {"front_photo": "front-view", "rear_photo": "rear-view", "back_photo": "back-view",
                  "left_photo": "left-side", "right_photo": "right-side", "chassis_photo": "chassis-number"}
SIDE_HELP = "Left and right mean the vehicle’s own sides, as if you were sitting in the driver’s seat facing forward."

# Result codes
ACCEPTED, WRONG_VIEW, WRONG_IMAGE, NO_VEHICLE = "ACCEPTED", "WRONG_VIEW", "WRONG_IMAGE", "NO_VEHICLE"
LOW_CONFIDENCE, LOW_QUALITY, UNREADABLE, UNAVAILABLE, NOT_VERIFIED = (
    "LOW_CONFIDENCE", "LOW_QUALITY", "UNREADABLE", "UNAVAILABLE", "NOT_VERIFIED")

MODEL_MAX_SIDE = 1568          # long edge sent to the vision model (keeps cost and latency low)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def validate_vehicle_image(image_path, expected_view, slot=None):
    """Validate the photo at image_path against expected_view (e.g. "REAR")."""
    with open(image_path, "rb") as fh:
        return validate_vehicle_image_bytes(fh.read(), expected_view, slot=slot)


def validate_slot(slot, data):
    """Validate image bytes uploaded for a photo slot such as "rear_photo"."""
    return validate_vehicle_image_bytes(data, SLOT_EXPECTED_VIEW[slot], slot=slot)


def validate_slots(images):
    """Validate several slots in parallel. images: {slot: bytes} → {slot: result}."""
    with ThreadPoolExecutor(max_workers=max(1, min(5, len(images)))) as pool:
        futures = {slot: pool.submit(validate_slot, slot, data) for slot, data in images.items()}
        return {slot: f.result() for slot, f in futures.items()}


def validate_vehicle_image_bytes(data, expected_view, slot=None):
    expected_view = str(expected_view).upper()
    if expected_view not in VEHICLE_VIEWS | {CHASSIS}:
        raise ValueError(f"Unknown expected view: {expected_view}")
    digest = hashlib.sha256(data).hexdigest()

    # 1. Local quality checks ------------------------------------------------
    try:
        image = _open_image(data)
    except ValueError as exc:
        return _result(False, expected_view, None, None, LOW_QUALITY, "Photo can’t be read", str(exc),
                       details={"sha256": digest})
    quality = assess_quality(image, expect_vehicle=(expected_view != CHASSIS))
    if not quality["ok"]:
        return _result(False, expected_view, None, None, LOW_QUALITY, "Photo quality is too low", quality["message"],
                       details={"sha256": digest, "quality": quality["metrics"], "quality_problem": quality["problem"]})

    # 2. AI check (or explicit opt-out) -----------------------------------------
    ai_ready, ai_reason = _ai_ready()
    if not ai_ready and config.PHOTO_VALIDATION_MODE != "required":
        return _result(True, expected_view, None, None, NOT_VERIFIED, "Photo added",
                       "Our claims team will review this photo.",
                       details={"sha256": digest, "quality": quality["metrics"], "ai": ai_reason})

    try:
        provider = vision_providers.get_provider()
        obs = _cached_observation(digest)
        if obs is None:
            obs = normalize_observation(provider.analyze(_jpeg_for_model(image)))
            _remember_observation(digest, obs)
            _last_error.clear()
    except VisionProviderError as exc:
        log.warning("Photo check unavailable: %s", exc)
        _last_error.update(message=str(exc), at=datetime.now().strftime("%d %b %Y, %I:%M %p"))
        return _result(False, expected_view, None, None, UNAVAILABLE, "We couldn’t check this photo",
                       "Our photo check is temporarily unavailable. Please try again in a moment.",
                       details={"sha256": digest, "error": str(exc)}, retryable=True)

    result = decide(expected_view, obs, slot=slot)
    result["details"].update({"sha256": digest, "quality": quality["metrics"],
                              "provider": getattr(provider, "name", "?"), "model": getattr(provider, "model", "")})
    return result


_last_error = {}


def _ai_ready():
    """(ready, reason). Never raises."""
    if config.PHOTO_VALIDATION_MODE == "off":
        return False, "disabled (PHOTO_VALIDATION_MODE=off)"
    try:
        return vision_providers.get_provider().status()
    except VisionProviderError as exc:
        return False, str(exc)


def validation_status():
    """Summary for the startup banner and admin settings page."""
    mode = config.PHOTO_VALIDATION_MODE
    info = {"mode": mode, "threshold": config.CONFIDENCE_THRESHOLD, "provider": config.PHOTO_VALIDATION_PROVIDER}
    if mode == "off":
        info.update(ready=False, active=False, summary="OFF — photos are not AI-checked (PHOTO_VALIDATION_MODE=off)")
        return info
    if mode != "required":
        ready, why = _ai_ready()
        if not ready:
            info.update(ready=False, active=False,
                        summary=f"OFF — {why}. Photos are accepted without the AI check (marked NOT VERIFIED).")
            return info
    try:
        provider = vision_providers.get_provider()
        ready, why = provider.status()
        info.update(model=getattr(provider, "model", ""))
    except VisionProviderError as exc:
        ready, why = False, str(exc)
    if _last_error:
        info["last_error"] = f"Last problem ({_last_error['at']}): {_last_error['message']}"
    info.update(ready=ready, active=True,
                summary=(f"ON — {why}, confidence ≥ {config.CONFIDENCE_THRESHOLD:.0%}" if ready
                         else f"NOT READY — {why}. Customers can’t submit photos until this is fixed."))
    return info


# ---------------------------------------------------------------------------
# Decision rules (pure, unit-testable)
# ---------------------------------------------------------------------------
def detect_view(obs):
    """Turn the model's observation into FRONT/REAR/LEFT_SIDE/RIGHT_SIDE/CHASSIS/UNKNOWN.

    Left/right is derived from where the vehicle's nose points in the image:
    standing at the vehicle's LEFT side, its front is on your left, so the nose
    points to IMAGE_LEFT. This is more reliable than asking the model "which side?".
    """
    if obs["photo_type"] == "CHASSIS_VIN_CLOSEUP":
        return CHASSIS
    if obs["photo_type"] in ("OTHER", "VEHICLE_INTERIOR") or not obs["vehicle_present"]:
        return UNKNOWN
    view = obs["dominant_view"]
    if view in (FRONT, REAR):
        return view
    if view == "SIDE":
        return {"IMAGE_LEFT": LEFT_SIDE, "IMAGE_RIGHT": RIGHT_SIDE}.get(obs["nose_direction"], UNKNOWN)
    return UNKNOWN


def decide(expected_view, obs, slot=None):
    threshold = config.CONFIDENCE_THRESHOLD
    conf = obs["confidence"]
    detected = detect_view(obs)
    slot_name, verified_title = SLOT_WORDING.get(slot) or VIEW_WORDING[expected_view]
    details = {k: obs[k] for k in ("observations", "photo_type", "dominant_view", "nose_direction", "angle",
                                   "vin_text", "vin_legibility", "image_quality", "quality_issues")}
    details["plate_text_lines"] = obs.get("plate_text_lines") or []
    details["engine_number_text"] = obs.get("engine_number_text")
    details["license_plate_text"] = obs.get("license_plate_text")

    def out(valid, code, title, message):
        return _result(valid, expected_view, detected, conf, code, title, message, details=details)

    no_vehicle = obs["photo_type"] == "OTHER" or (not obs["vehicle_present"] and detected != CHASSIS)

    # Unusable photo according to the model.
    if obs["image_quality"] == "POOR":
        return out(False, LOW_QUALITY, "Photo quality is too low",
                   _quality_text(obs["quality_issues"], vehicle=(expected_view != CHASSIS)))

    # --- Chassis / VIN slot ---------------------------------------------------
    if expected_view == CHASSIS:
        if detected == CHASSIS:
            if conf < threshold:
                return out(False, LOW_CONFIDENCE, "Unable to confidently verify this image",
                           "Please upload a clearer, closer photo of the chassis number.")
            if obs["vin_legibility"] == "ILLEGIBLE":
                return out(False, UNREADABLE, "Chassis number isn’t readable",
                           "Please retake the photo closer, in good light and without glare, so every character can be read.")
            return out(True, ACCEPTED, verified_title, "Photo accepted")
        if conf >= threshold or no_vehicle or detected in VEHICLE_VIEWS:
            return out(False, WRONG_IMAGE, "Incorrect image",
                       "This does not appear to contain a chassis/VIN number. Please upload a clear photo of the chassis number.")
        return out(False, LOW_CONFIDENCE, "Unable to confidently verify this image",
                   "Please upload a clearer photo of the chassis number.")

    # --- Vehicle view slots ----------------------------------------------------
    if detected == CHASSIS:
        return out(False, WRONG_IMAGE, "Incorrect image",
                   f"This looks like a close-up of a chassis number. Please upload the {slot_name} of your vehicle.")
    if no_vehicle:
        return out(False, NO_VEHICLE, "No vehicle found",
                   "We couldn’t see a vehicle in this photo. Please upload a photo with your vehicle clearly visible.")
    if obs["photo_type"] == "VEHICLE_INTERIOR":
        return out(False, WRONG_IMAGE, "Incorrect image",
                   f"This photo shows the inside of the vehicle. Please upload the {slot_name} from outside.")
    if conf < threshold or detected == UNKNOWN:
        return out(False, LOW_CONFIDENCE, "Unable to confidently verify this image",
                   f"Please upload a clearer photo that clearly shows the {slot_name}.")
    if detected == expected_view:
        return out(True, ACCEPTED, verified_title, "Photo accepted")
    if {detected, expected_view} == {LEFT_SIDE, RIGHT_SIDE}:
        wanted = "LEFT" if expected_view == LEFT_SIDE else "RIGHT"
        got = "RIGHT" if wanted == "LEFT" else "LEFT"
        return out(False, WRONG_VIEW, "Incorrect side view",
                   f"This photo appears to show the {got} side of the vehicle. Please upload a photo showing "
                   f"the {wanted} side. {SIDE_HELP}")
    return out(False, WRONG_VIEW, "Incorrect vehicle angle",
               f"You selected a {SLOT_ADJECTIVE.get(slot) or slot_name} photo, but this image appears to show "
               f"{VIEW_PHRASE[detected]}. "
               f"Please upload the {slot_name}.")


# ---------------------------------------------------------------------------
# Local image-quality checks (Pillow only)
# ---------------------------------------------------------------------------
def _open_image(data):
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = ImageOps.exif_transpose(img)        # respect phone rotation
        return img.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise ValueError("This file isn’t a readable photo. Please take or choose the photo again.") from exc


def assess_quality(img, expect_vehicle=True):
    """Return {"ok", "problem", "message", "metrics"} for obviously unusable photos only."""
    w, h = img.size
    small = img.convert("L")
    small.thumbnail((640, 640))
    stat = ImageStat.Stat(small)
    brightness, contrast = stat.mean[0], stat.stddev[0]
    lap = small.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128))
    lap = lap.crop((2, 2, lap.width - 2, lap.height - 2))   # Pillow leaves border pixels unfiltered
    sharpness = ImageStat.Stat(lap).stddev[0] ** 2
    metrics = {"width": w, "height": h, "brightness": round(brightness, 1),
               "contrast": round(contrast, 1), "sharpness": round(sharpness, 1)}
    subject = "the vehicle clearly visible" if expect_vehicle else "the chassis number clearly visible"

    def bad(problem, why):
        return {"ok": False, "problem": problem, "metrics": metrics,
                "message": f"{why} Please upload a clearer image with {subject}."}

    if min(w, h) < config.MIN_IMAGE_SIDE_PX:
        return bad("low_resolution", "This photo’s resolution is too low.")
    if brightness < 12 or (contrast < 6 and brightness < 60):
        return bad("black", "This photo is almost completely black.")
    if contrast < 4:
        return bad("blank", "This photo appears to be blank or completely out of focus.")
    if brightness < config.MIN_BRIGHTNESS:
        return bad("too_dark", "This photo is too dark.")
    if brightness > 248:
        return bad("overexposed", "This photo is too bright to see any detail.")
    if sharpness < config.MIN_SHARPNESS:
        return bad("blurry", "This photo is too blurry.")
    return {"ok": True, "problem": None, "message": "", "metrics": metrics}


def _quality_text(issues, vehicle=True):
    reasons = {"blurry": "It looks blurry.", "too_dark": "It looks too dark.", "overexposed": "It looks too bright.",
               "glare": "There is strong glare.", "obstructed": "Something is blocking the view.",
               "vehicle_cut_off": "Part of the vehicle is cut off.", "too_far": "The vehicle is too far away."}
    why = " ".join(reasons[i] for i in issues if i in reasons)
    subject = "the vehicle clearly visible" if vehicle else "the chassis number clearly visible"
    return (why + " " if why else "") + f"Please upload a clearer image with {subject}."


def _jpeg_for_model(img):
    copy = img.copy()
    copy.thumbnail((MODEL_MAX_SIDE, MODEL_MAX_SIDE))
    buf = io.BytesIO()
    copy.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _result(valid, expected, detected, confidence, code, title, message, details=None, retryable=False):
    status = {ACCEPTED: "ACCEPTED", NOT_VERIFIED: "NOT_VERIFIED", UNAVAILABLE: "ERROR"}.get(code, "REJECTED")
    return {
        "valid": bool(valid),
        "expected_view": expected,
        "detected_view": detected,
        "confidence": confidence,
        "status": status,
        "code": code,
        "title": title,
        "message": message,
        "retryable": retryable,
        "validated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "details": dict(details or {}),
    }


# Same photo bytes → same observation: retries and the re-check at submit don't pay for a second AI call.
_cache = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 256


def _cached_observation(digest):
    with _cache_lock:
        obs = _cache.get(digest)
        if obs is not None:
            _cache.move_to_end(digest)
        return dict(obs) if obs else None


def _remember_observation(digest, obs):
    with _cache_lock:
        _cache[digest] = dict(obs)
        _cache.move_to_end(digest)
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)


def clear_cache():
    with _cache_lock:
        _cache.clear()
