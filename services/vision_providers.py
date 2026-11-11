"""Vision-model providers for vehicle photo analysis.

The validator never talks to an AI vendor directly; it asks a provider to
*describe* a photo using OBSERVATION_SCHEMA and then makes the accept/reject
decision itself. To use a different model or vendor, write a class with the
same two methods as VisionProvider, add it to PROVIDERS, and set
PHOTO_VALIDATION_PROVIDER in config.py / .env.

The model is never told which photo slot the customer is filling, so it can't
be nudged into agreeing with the customer: it reports what it sees.
"""
import base64
import logging
import threading

import config

log = logging.getLogger(__name__)


class VisionProviderError(RuntimeError):
    """The provider couldn't analyze the photo (network, auth, timeout, bad reply)."""


# ---------------------------------------------------------------------------
# What every provider must return (and what the model is asked to fill in)
# ---------------------------------------------------------------------------
VIEW_ENUM = ["FRONT", "REAR", "SIDE", "NONE", "UNCLEAR"]
NOSE_ENUM = ["IMAGE_LEFT", "IMAGE_RIGHT", "TOWARD_CAMERA", "AWAY_FROM_CAMERA", "UNCLEAR"]
QUALITY_ISSUES = ["blurry", "too_dark", "overexposed", "glare", "obstructed", "vehicle_cut_off",
                  "too_far", "screen_or_printout"]

OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "string",
            "description": "One or two plain sentences on what the photo shows (for the claims team).",
        },
        "photo_type": {
            "type": "string",
            "enum": ["VEHICLE_EXTERIOR", "CHASSIS_VIN_CLOSEUP", "VEHICLE_INTERIOR", "OTHER"],
            "description": "VEHICLE_EXTERIOR: the outside of a real car/SUV/van/truck is the main subject. "
                           "CHASSIS_VIN_CLOSEUP: a close-up of a stamped/engraved chassis number, VIN plate, "
                           "VIN sticker or VIN label. OTHER: anything else (people, animals, documents, scenery...).",
        },
        "vehicle_present": {"type": "boolean",
                             "description": "True if a real vehicle (or part of one) is clearly the main subject."},
        "front_features_visible": {"type": "array", "items": {"type": "string"},
                                   "description": "e.g. headlights, grille, front bumper, front windshield, front plate."},
        "rear_features_visible": {"type": "array", "items": {"type": "string"},
                                  "description": "e.g. tail lights, rear windshield, boot/trunk/tailgate, rear bumper, rear plate, exhaust."},
        "dominant_view": {
            "type": "string", "enum": VIEW_ENUM,
            "description": "Which face of the vehicle dominates the photo: FRONT, REAR or SIDE. For an angled "
                           "(three-quarter) photo choose the face that takes up most of the visible body. "
                           "NONE if no vehicle; UNCLEAR if you cannot tell.",
        },
        "nose_direction": {
            "type": "string", "enum": NOSE_ENUM,
            "description": "Where the FRONT (nose) of the vehicle points within the image. For side and angled "
                           "photos give the horizontal direction (IMAGE_LEFT or IMAGE_RIGHT). For a straight front "
                           "view use TOWARD_CAMERA, for a straight rear view AWAY_FROM_CAMERA.",
        },
        "angle": {"type": "string", "enum": ["STRAIGHT_ON", "THREE_QUARTER", "UNCLEAR"]},
        "vin_visible": {"type": "boolean", "description": "True if a chassis number / VIN is visible."},
        "vin_legibility": {"type": "string", "enum": ["CLEAR", "PARTIAL", "ILLEGIBLE", "NOT_APPLICABLE"]},
        "plate_text_lines": {"type": "array", "items": {"type": "string"},
                             "description": "For a chassis/VIN plate or label: every line of text exactly as printed, "
                                            "top to bottom (labels included). Empty list for other photos."},
        "vin_text": {"type": "string", "nullable": True,
                     "description": "The CHASSIS number / VIN only (usually 17 characters), exactly as printed, "
                                    "else null. Never put the engine number here."},
        "engine_number_text": {"type": "string", "nullable": True,
                               "description": "The ENGINE number if printed on the plate (e.g. after 'ENGINE NO.'), else null."},
        "license_plate_text": {"type": "string", "nullable": True,
                               "description": "If a vehicle registration number plate is visible (e.g. front or rear plate), the exact registration text, else null."},
        "image_quality": {"type": "string", "enum": ["GOOD", "ACCEPTABLE", "POOR"],
                          "description": "POOR only if the photo is unusable for an insurance assessment."},
        "quality_issues": {"type": "array", "items": {"type": "string", "enum": QUALITY_ISSUES}},
        "confidence": {"type": "number",
                       "description": "0 to 1: how sure you are about photo_type and dominant_view (and, for side "
                                      "views, nose_direction). Be honest; use below 0.8 when unsure."},
    },
    "required": ["observations", "photo_type", "vehicle_present", "front_features_visible",
                 "rear_features_visible", "dominant_view", "nose_direction", "angle", "vin_visible",
                 "vin_legibility", "plate_text_lines", "vin_text", "engine_number_text", "license_plate_text",
                 "image_quality", "quality_issues", "confidence"],
     
}

ANALYSIS_INSTRUCTIONS = """You are a vehicle-inspection photo analyst for a car insurance company.
A customer took ONE photo for a damage claim. Describe objectively what the photo shows by filling in
the requested fields. You are not told which photo was requested; do not guess, just report.

How to decide the view:
- Look for evidence first. FRONT evidence: headlights, grille, front bumper, front windshield, bonnet/hood.
  REAR evidence: tail lights, rear windshield, boot/trunk lid or tailgate, rear bumper, rear number plate, exhaust.
- dominant_view is the face that takes up most of the visible vehicle body. A slightly angled or
  three-quarter photo still counts as FRONT/REAR/SIDE if that face clearly dominates.
- nose_direction is purely about the image: which way the vehicle's front points in the picture.
  In a side photo, find the end with the headlights/grille and say whether it is toward the left or the
  right edge of the image. Never mirror or flip the image in your head.
- A close-up of a stamped/engraved chassis number, a VIN plate, VIN sticker or VIN label (e.g. on the
  door pillar, under the bonnet, on the windshield corner or on the frame) is CHASSIS_VIN_CLOSEUP.
  Transcribe every line of the plate into plate_text_lines. Put ONLY the chassis number / VIN into vin_text
  (a VIN is 17 characters and never contains I, O or Q). Plates often also show an ENGINE number (after
  "ENGINE NO." or similar): put that in engine_number_text and never in vin_text.
- If the photo is not of a vehicle (a person, animal, document, scenery, screenshot of something else),
  use photo_type OTHER, vehicle_present false, dominant_view NONE.
- Mark image_quality POOR only when the photo is genuinely unusable (extremely blurry, almost black,
  vehicle mostly hidden). Ordinary phone photos with normal lighting are GOOD or ACCEPTABLE.
- confidence must reflect real certainty. Do not inflate it."""


def normalize_observation(raw):
    """Coerce a provider's raw dict into a safe, fully-populated observation."""
    if not isinstance(raw, dict):
        raise VisionProviderError("Vision model returned no structured result.")

    def enum(value, allowed, default):
        v = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
        return v if v in allowed else default

    def str_list(value):
        return [str(x)[:60] for x in value][:12] if isinstance(value, list) else []

    try:
        conf = float(raw.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if conf > 1.0 and conf <= 100.0:      # some models answer in percent
        conf = conf / 100.0
    conf = max(0.0, min(1.0, conf))

    vin_text = raw.get("vin_text")
    vin_text = str(vin_text).strip()[:40] if vin_text not in (None, "", "null") else None

    issues = [i for i in (str(x).strip().lower() for x in (raw.get("quality_issues") or [])) if i in QUALITY_ISSUES]

    return {
        "observations": str(raw.get("observations") or "")[:400],
        "photo_type": enum(raw.get("photo_type"), {"VEHICLE_EXTERIOR", "CHASSIS_VIN_CLOSEUP", "VEHICLE_INTERIOR", "OTHER"}, "OTHER"),
        "vehicle_present": bool(raw.get("vehicle_present")),
        "front_features_visible": str_list(raw.get("front_features_visible")),
        "rear_features_visible": str_list(raw.get("rear_features_visible")),
        "dominant_view": enum(raw.get("dominant_view"), set(VIEW_ENUM), "UNCLEAR"),
        "nose_direction": enum(raw.get("nose_direction"), set(NOSE_ENUM), "UNCLEAR"),
        "angle": enum(raw.get("angle"), {"STRAIGHT_ON", "THREE_QUARTER", "UNCLEAR"}, "UNCLEAR"),
        "vin_visible": bool(raw.get("vin_visible")),
        "vin_legibility": enum(raw.get("vin_legibility"), {"CLEAR", "PARTIAL", "ILLEGIBLE", "NOT_APPLICABLE"}, "NOT_APPLICABLE"),
        "vin_text": vin_text,
        "engine_number_text": (str(raw.get("engine_number_text")).strip()[:40]
                               if raw.get("engine_number_text") not in (None, "", "null") else None),
        "license_plate_text": (str(raw.get("license_plate_text")).strip()[:30]
                               if raw.get("license_plate_text") not in (None, "", "null") else None),
        "plate_text_lines": [str(x)[:120] for x in (raw.get("plate_text_lines") or [])][:30]
                            if isinstance(raw.get("plate_text_lines"), list) else [],
        "image_quality": enum(raw.get("image_quality"), {"GOOD", "ACCEPTABLE", "POOR"}, "ACCEPTABLE"),
        "quality_issues": issues,
        "confidence": round(conf, 3),
    }


# ---------------------------------------------------------------------------
# Damage detection (claims-team analysis; separate from the photo-angle check)
# ---------------------------------------------------------------------------
DAMAGE_TYPES = [
    "DENT",
    "SCRATCH",
    "CRACK",
    "PAINT_DAMAGE",
    "BUMPER_DAMAGE",
    "GLASS_DAMAGE",
    "LAMP_DAMAGE",
    "PANEL_DEFORMATION",
    "BROKEN_OR_MISSING_PART",
    "MIRROR_DAMAGE",
    "TYRE_OR_WHEEL_DAMAGE",
    "OTHER",
]
SEVERITIES = ["MINOR", "MODERATE", "SEVERE", "UNCERTAIN"]
VEHICLE_PARTS = [
    "FRONT_BUMPER", "REAR_BUMPER", "BONNET", "BOOT_TAILGATE", "ROOF", "FRONT_WINDSHIELD", "REAR_WINDSHIELD",
    "LEFT_FRONT_DOOR", "LEFT_REAR_DOOR", "RIGHT_FRONT_DOOR", "RIGHT_REAR_DOOR",
    "LEFT_FRONT_FENDER", "RIGHT_FRONT_FENDER", "LEFT_REAR_QUARTER", "RIGHT_REAR_QUARTER",
    "LEFT_HEADLIGHT", "RIGHT_HEADLIGHT", "LEFT_TAIL_LIGHT", "RIGHT_TAIL_LIGHT",
    "LEFT_MIRROR", "RIGHT_MIRROR", "SIDE_WINDOW", "WHEEL_TYRE", "OTHER",
]

DAMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "image_assessable": {"type": "boolean",
                             "description": "False if the photo is too unclear to judge damage at all."},
        "notes": {"type": "string", "description": "One short sentence summarizing the visible condition."},
        "damages": {
            "type": "array",
            "description": "Every distinct area of VISIBLE physical damage. Empty list if none is visible.",
            "items": {
                "type": "object",
                "properties": {
                    "damage_type": {"type": "string", "enum": DAMAGE_TYPES},
                    "vehicle_part": {"type": "string", "enum": VEHICLE_PARTS},
                    "location": {"type": "string",
                                 "description": "Short human description, e.g. 'front bumper, left corner'."},
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "confidence": {"type": "number", "description": "0 to 1, honest certainty this is real damage."},
                    "box": {
                        "type": "object",
                        "description": "Tight box around the damaged area, as fractions (0 to 1) of the image "
                                       "width/height, origin at the top-left corner.",
                        "properties": {"x_min": {"type": "number"}, "y_min": {"type": "number"},
                                       "x_max": {"type": "number"}, "y_max": {"type": "number"}},
                        "required": ["x_min", "y_min", "x_max", "y_max"],
                    },
                    "reason": {"type": "string", "description": "What visual evidence shows the damage."},
                },
                "required": ["damage_type", "vehicle_part", "severity", "reason"],
            },
        },
    },
    "required": ["image_assessable", "notes", "damages"],
}

DAMAGE_INSTRUCTIONS = """You are a motor-insurance damage assessor reviewing ONE customer photo of their vehicle.
Your job is to identify and LIST EVERY visible damage area in the photo. Being thorough is critical.

Supported damage types:
- DENT: depressions, dents on body panels
- SCRATCH: surface scratches, scrapes, or gouges
- CRACK: cracked panels, bumpers, lights, or glass
- PAINT_DAMAGE: scuffed paint, paint chips, flaking, or peeling paint
- BUMPER_DAMAGE: pushed in, cracked, torn, hanging, detached, or scuffed bumper
- GLASS_DAMAGE: chipped, cracked, or shattered windscreen or window
- LAMP_DAMAGE: cracked, broken, or shattered headlight, tail light, or indicator
- PANEL_DEFORMATION: crumpled, bent, folded, or severely distorted panel
- BROKEN_OR_MISSING_PART: detached trim, missing grille, broken handle, or missing piece
- MIRROR_DAMAGE: cracked, broken, or dislodged wing mirror
- TYRE_OR_WHEEL_DAMAGE: flat tyre, tyre gash, scraped alloy, or bent wheel rim
- OTHER: any other physical vehicle damage

Rules:
- Report only what is visible in this photo.
- Never describe damage in notes while returning an empty damages list. If damage is noted, list it in damages.
- A crumpled, crushed, deformed, or bent panel is a DENT or PANEL_DEFORMATION.
- Use "OTHER" or severity "UNCERTAIN" when unsure of the damage type or severity.
- Ignore dirt, dust, water spots, soft reflections, shadows, stickers, normal panel gaps, and design lines.
- vehicle_part and location use the VEHICLE'S OWN left/right (as seen from the driver's seat facing forward).
- Boxes are fractions of the image (0 = left/top edge, 1 = right/bottom edge), tight around the damaged area only.
- severity: MINOR = light surface scratch/scuff, small shallow dent; MODERATE = deep scratch through paint,
  noticeable dent, cracked panel; SEVERE = torn/broken/missing part, large deformation, shattered glass.
  Use UNCERTAIN when the photo does not let you judge severity.
- confidence is your honest certainty that this is genuine damage. Never fabricate confidence.
- Return strict JSON; do not wrap output in markdown fences.
"""


def notes_describe_damage(notes):
    """Return True if notes text describes visible vehicle damage."""
    if not notes:
        return False
    import re
    text = str(notes).strip().lower()

    strong_damage = [
        "smashed", "crumpled", "crushed", "deformed", "broken", "torn", "shattered",
        "buckled", "caved", "heavy damage", "severe damage", "impact", "collision",
        "missing", "cracked", "dented", "scratched", "scuffed", "dents", "scratches",
        "cracks", "deformation", "puncture", "flat tyre", "flat tire", "body damage",
        "front-end damage", "rear-end damage", "side damage"
    ]
    if any(w in text for w in strong_damage):
        return True

    # Strip out all 'no / not / free of / without ... damage / dents / etc'
    cleaned = re.sub(r"\b(?:no|not|free of|without)\s+(?:[\w]+\s+){0,4}(?:damage|dents?|scratches?|cracks?)\b", "", text)
    cleaned = re.sub(r"\bundamaged\b", "", cleaned)
    cleaned = re.sub(r"\bintact\b", "", cleaned)
    cleaned = re.sub(r"\bclean\b", "", cleaned)
    cleaned = re.sub(r"\bgood condition\b", "", cleaned)

    damage_keywords = [
        "dent", "scratch", "crack", "paint damage", "scuff", "bumper damage",
        "glass damage", "headlight damage", "tail light damage", "lamp damage",
        "mirror damage", "tyre damage", "tire damage", "wheel damage", "gash",
        "deformation", "damage"
    ]
    for w in damage_keywords:
        if re.search(r"\b" + re.escape(w) + r"\b", cleaned):
            return True

    return False


def normalize_damage_report(raw, max_items=30):
    """Coerce a provider's damage report into a safe structure with valid boxes."""
    if not isinstance(raw, dict):
        raise VisionProviderError("Vision model returned no damage report.")

    def enum(value, allowed, default):
        v = str(value or "").strip().upper().replace(" ", "_").replace("-", "_")
        return v if v in allowed else default

    def num(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def confidence(value):
        if value is None or value == "":
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f / 100.0 if 1.0 < f <= 100.0 else f            # some models answer in percent

    items = []
    for d in (raw.get("damages") or [])[:max_items]:
        if not isinstance(d, dict):
            continue
        box = d.get("box") or d.get("box_2d") or {}
        has_box = False
        x0 = y0 = x1 = y1 = 0.0
        if isinstance(box, (list, tuple)) and len(box) >= 4:
            x0, y0, x1, y1 = (max(0.0, min(1.0, num(v))) for v in box[:4])
            x0, x1 = sorted((x0, x1))
            y0, y1 = sorted((y0, y1))
            if x1 - x0 >= 0.005 and y1 - y0 >= 0.005:
                has_box = True
        elif isinstance(box, dict):
            if "x" in box and "y" in box and "w" in box and "h" in box:
                bx, by, bw, bh = num(box.get("x")), num(box.get("y")), num(box.get("w")), num(box.get("h"))
                x0, y0, x1, y1 = max(0.0, bx), max(0.0, by), min(1.0, bx + bw), min(1.0, by + bh)
            else:
                x0, y0, x1, y1 = (max(0.0, min(1.0, num(box.get(k)))) for k in ("x_min", "y_min", "x_max", "y_max"))
            x0, x1 = sorted((x0, x1))
            y0, y1 = sorted((y0, y1))
            if x1 - x0 >= 0.005 and y1 - y0 >= 0.005:
                has_box = True

        conf = confidence(d.get("confidence"))
        if conf is not None:
            conf = round(max(0.0, min(1.0, conf)), 3)
        items.append({
            "damage_type": enum(d.get("damage_type"), set(DAMAGE_TYPES), "OTHER"),
            "vehicle_part": enum(d.get("vehicle_part"), set(VEHICLE_PARTS), "OTHER"),
            "location": str(d.get("location") or "").strip()[:80],
            "severity": enum(d.get("severity"), set(SEVERITIES), "UNCERTAIN"),
            "confidence": conf,
            "box": {"x": round(x0, 4), "y": round(y0, 4), "w": round(x1 - x0, 4), "h": round(y1 - y0, 4)} if has_box else {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
            "reason": str(d.get("reason") or "").strip()[:200],
        })

    notes = str(raw.get("notes") or "")[:300]
    has_damage_in_notes = notes_describe_damage(notes)
    inconsistent = bool(raw.get("inconsistent", False))
    if not items and has_damage_in_notes:
        inconsistent = True

    return {
        "image_assessable": bool(raw.get("image_assessable", True)),
        "notes": notes,
        "damages": items,
        "inconsistent": inconsistent,
        "notes_has_damage": has_damage_in_notes,
    }


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class VisionProvider:
    """Interface. Subclasses implement status() and analyze()."""
    name = "base"
    model = ""

    def status(self):
        """Return (ready: bool, human-readable reason)."""
        raise NotImplementedError

    def analyze(self, jpeg_bytes):
        """Return a raw observation dict for one JPEG image. Raise VisionProviderError on failure."""
        raise NotImplementedError

    def detect_damage(self, jpeg_bytes, view_hint=""):
        """Return a raw damage report (DAMAGE_SCHEMA) for one JPEG image."""
        raise VisionProviderError(f"Provider '{self.name}' does not support damage detection.")


class GeminiVisionProvider(VisionProvider):
    """Gemini vision via Google GenAI SDK, with JSON-schema structured output."""
    name = "gemini"

    def __init__(self, api_key=None, model=None, timeout=None, client=None):
        self.api_key = api_key if api_key is not None else getattr(config, "GEMINI_API_KEY", "")
        self.model = model or getattr(config, "GEMINI_MODEL", "gemini-3.5-flash-lite")
        self.timeout = timeout or config.PHOTO_VALIDATION_TIMEOUT
        self._client = client

    def status(self):
        """Return (ready: bool, human-readable reason)."""
        if self._client is not None:
            return True, f"Gemini ({self.model})"
        if not self.api_key:
            return False, ("AI analysis unavailable - the Gemini API key is missing "
                           "(set GEMINI_API_KEY in .env).")
        try:
            from google import genai  # noqa: F401
        except ImportError:
            return False, ("AI analysis unavailable - the 'google-genai' package is not "
                           "installed in this virtual environment.")
        return True, f"Gemini ({self.model})"

    def _get_client(self):
        if self._client is None:
            from services.gemini_vehicle_ocr import get_gemini_client
            self._client = get_gemini_client(api_key=self.api_key)
        return self._client

    def analyze(self, jpeg_bytes):
        return self._structured(jpeg_bytes, ANALYSIS_INSTRUCTIONS, OBSERVATION_SCHEMA,
                                "Analyze this customer photo.", max_tokens=1500)

    def detect_damage(self, jpeg_bytes, view_hint=""):
        prompt = "Find all visible damage in this vehicle photo."
        if view_hint:
            prompt += f" The customer took this photo of the {view_hint}."
        raw = self._structured(jpeg_bytes, DAMAGE_INSTRUCTIONS, DAMAGE_SCHEMA, prompt, max_tokens=4096)
        if not isinstance(raw, dict):
            raw = {"image_assessable": True, "notes": "", "damages": []}

        report = normalize_damage_report(raw)
        # Consistency guard: if text summary/notes describe damage but structured list is empty
        if not report["damages"] and report.get("notes_has_damage"):
            log.warning("Inconsistent Gemini damage report: notes describe damage %r but damages list is empty. Retrying with stricter prompt...", report.get("notes"))
            retry_prompt = (
                f"{prompt}\n\nYour summary mentions damage: \"{report.get('notes')}\". "
                "Your summary mentions damage, so list every damaged part as a separate entry with "
                "damage_type, vehicle_part, severity, and reason."
            )
            try:
                retry_raw = self._structured(jpeg_bytes, DAMAGE_INSTRUCTIONS, DAMAGE_SCHEMA, retry_prompt, max_tokens=4096)
                if isinstance(retry_raw, dict):
                    retry_report = normalize_damage_report(retry_raw)
                    if retry_report["damages"]:
                        log.info("Retry succeeded with %d damages detected.", len(retry_report["damages"]))
                        return retry_raw
                log.warning("Retry still returned empty damages list despite damage described in notes. Marking as inconsistent.")
                raw["inconsistent"] = True
                return raw
            except Exception as exc:
                log.warning("Damage detection retry failed: %s", exc)
                raw["inconsistent"] = True
                return raw

        return raw

    def _structured(self, jpeg_bytes, instructions, schema, prompt, max_tokens):
        ready, why = self.status()
        if not ready:
            raise VisionProviderError(why)

        def _call_gemini():
            from google.genai import types
            return self._get_client().models.generate_content(
                model=self.model,
                contents=[
                    types.Part.from_bytes(
                        data=jpeg_bytes,
                        mime_type="image/jpeg",
                    ),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    system_instruction=instructions,
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0,
                    max_output_tokens=max_tokens,
                ),
            )

        try:
            response = _call_gemini()
        except Exception as exc:
            reason = _explain_gemini_error(exc, self.model)
            log.warning("Gemini vision request failed: %s | %s", reason, exc)
            raise VisionProviderError(reason) from exc

        import json
        import re
        text = getattr(response, "text", "") or ""
        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        log.info("RAW Gemini response: %s", text)

        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:
            log.warning("Unparseable vision reply (retrying once): %r", text[:300])
            try:
                response2 = _call_gemini()
                text2 = getattr(response2, "text", "") or ""
                text2 = re.sub(r"^```(?:json)?\s*", "", text2.strip(), flags=re.IGNORECASE)
                text2 = re.sub(r"\s*```$", "", text2)
                log.info("RAW Gemini retry response: %s", text2)
                return json.loads(text2)
            except Exception as exc2:
                log.warning("Vision reply retry also failed: %s", exc2)
                raise VisionProviderError("Vision model returned an unreadable answer.") from exc2


def _explain_gemini_error(exc, model):
    """Turn a Gemini SDK exception into a plain-English reason for the server log and admin page."""
    name = type(exc).__name__
    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status in (401, 403) or "api_key" in text or "unauthenticated" in text or "permission" in text or "invalid api key" in text:
        return "the Gemini API key was rejected (check GEMINI_API_KEY in .env)"
    if status == 404 or "not found" in text:
        return f"model '{model}' isn't available to this API key (set GEMINI_MODEL in .env)"
    if "credit" in text or "billing" in text:
        return "the Gemini API account has billing or credit problems (check Google AI Studio console)"
    if status == 429 or "resource_exhausted" in text or "rate limit" in text or "quota" in text:
        return "too many requests to the Gemini API right now (rate limit)"
    if status in (500, 502, 503, 504) or "internal" in text or "unavailable" in text:
        return "the Gemini API is busy or having problems; try again shortly"
    if "connection" in text or "timeout" in text or "network" in text:
        return "couldn't reach Google Gemini API from this computer (check internet connection, firewall or VPN)"
    if status == 400 or "invalid_argument" in text:
        return f"the Gemini API rejected the request: {str(exc)[:200]}"
    return f"Gemini API error ({name})"


# Backward compatibility alias
AnthropicVisionProvider = GeminiVisionProvider

PROVIDERS = {
    "gemini": GeminiVisionProvider,
    "anthropic": GeminiVisionProvider,
}


_lock = threading.Lock()
_instance = None


def get_provider():
    """Shared provider instance chosen by config.PHOTO_VALIDATION_PROVIDER."""
    global _instance
    with _lock:
        if _instance is None:
            cls = PROVIDERS.get(config.PHOTO_VALIDATION_PROVIDER)
            if cls is None:
                raise VisionProviderError(f"Unknown PHOTO_VALIDATION_PROVIDER '{config.PHOTO_VALIDATION_PROVIDER}'")
            _instance = cls()
        return _instance


def set_provider(provider):
    """Swap the active provider (used by tests, or to plug in a custom model at startup)."""
    global _instance
    with _lock:
        _instance = provider
