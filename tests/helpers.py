"""Shared test helpers: synthetic photos and a scripted vision provider (no network)."""
import hashlib
import io
import random

from PIL import Image, ImageDraw

from services.vision_providers import VisionProvider, VisionProviderError


def make_photo(seed=0, size=(1280, 960), fmt="JPEG", exif_orientation=None):
    """A textured, well-lit photo-like image that passes the local quality checks."""
    rnd = random.Random(seed)
    w, h = size
    base = Image.new("RGB", size, (rnd.randint(60, 200), rnd.randint(60, 200), rnd.randint(60, 200)))
    noise = Image.effect_noise(size, 40).convert("RGB")
    img = Image.blend(base, noise, 0.35)
    d = ImageDraw.Draw(img)
    for _ in range(25):
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        d.rectangle([x, y, x + rnd.randint(20, 300), y + rnd.randint(20, 200)],
                    outline=(rnd.randint(0, 255),) * 3, width=3)
    buf = io.BytesIO()
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        img.save(buf, format=fmt, exif=exif.tobytes())
    else:
        img.save(buf, format=fmt, quality=90) if fmt == "JPEG" else img.save(buf, format=fmt)
    return buf.getvalue()


def obs(kind="rear", confidence=0.94, **overrides):
    """Model observations for common photo contents."""
    base = {
        "observations": f"test photo: {kind}", "photo_type": "VEHICLE_EXTERIOR", "vehicle_present": True,
        "front_features_visible": [], "rear_features_visible": [], "dominant_view": "UNCLEAR",
        "nose_direction": "UNCLEAR", "angle": "STRAIGHT_ON", "vin_visible": False,
        "vin_legibility": "NOT_APPLICABLE", "vin_text": None, "image_quality": "GOOD",
        "quality_issues": [], "confidence": confidence,
    }
    presets = {
        "rear": {"dominant_view": "REAR", "nose_direction": "AWAY_FROM_CAMERA",
                 "rear_features_visible": ["tail lights", "rear bumper", "boot"]},
        "rear_three_quarter": {"dominant_view": "REAR", "nose_direction": "IMAGE_LEFT", "angle": "THREE_QUARTER",
                               "rear_features_visible": ["tail lights", "rear windshield"]},
        "front": {"dominant_view": "FRONT", "nose_direction": "TOWARD_CAMERA",
                  "front_features_visible": ["headlights", "grille"]},
        "front_three_quarter": {"dominant_view": "FRONT", "nose_direction": "IMAGE_LEFT", "angle": "THREE_QUARTER",
                                "front_features_visible": ["headlights", "grille"]},
        "left": {"dominant_view": "SIDE", "nose_direction": "IMAGE_LEFT"},
        "right": {"dominant_view": "SIDE", "nose_direction": "IMAGE_RIGHT"},
        "side_unclear": {"dominant_view": "SIDE", "nose_direction": "UNCLEAR"},
        "random": {"photo_type": "OTHER", "vehicle_present": False, "dominant_view": "NONE"},
        "interior": {"photo_type": "VEHICLE_INTERIOR", "dominant_view": "NONE"},
        "chassis": {"photo_type": "CHASSIS_VIN_CLOSEUP", "vehicle_present": True, "dominant_view": "NONE",
                    "vin_visible": True, "vin_legibility": "CLEAR", "vin_text": "MA3EWDE1S00123456"},
        "chassis_partial": {"photo_type": "CHASSIS_VIN_CLOSEUP", "dominant_view": "NONE", "vin_visible": True,
                            "vin_legibility": "PARTIAL", "vin_text": None},
        "chassis_illegible": {"photo_type": "CHASSIS_VIN_CLOSEUP", "dominant_view": "NONE", "vin_visible": True,
                              "vin_legibility": "ILLEGIBLE", "vin_text": None},
        "poor": {"dominant_view": "REAR", "nose_direction": "AWAY_FROM_CAMERA", "image_quality": "POOR",
                 "quality_issues": ["blurry", "obstructed"]},
    }
    base.update(presets[kind])
    base.update(overrides)
    return base


class ScriptedProvider(VisionProvider):
    """Returns a pre-registered observation for each exact image (matched by the pixels sent).

    It looks only at the image bytes it receives, never at filenames or slots.
    """
    name = "scripted"
    model = "test-model"

    def __init__(self, default=None):
        self.damage_by_image = {}
        self.damage_calls = 0
        self.by_image = {}
        self.default = default
        self.calls = 0
        self.fail_with = None

    @staticmethod
    def _key(original_bytes):
        from services.vehicle_image_validator import _jpeg_for_model, _open_image
        return hashlib.sha256(_jpeg_for_model(_open_image(original_bytes))).hexdigest()

    def register(self, original_bytes, observation):
        self.by_image[self._key(original_bytes)] = observation

    def register_damage(self, original_bytes, damages, notes="test"):
        self.damage_by_image[self._key(original_bytes)] = {"image_assessable": True, "notes": notes,
                                                           "damages": damages}

    def detect_damage(self, jpeg_bytes, view_hint=""):
        self.damage_calls += 1
        if self.fail_with:
            raise VisionProviderError(self.fail_with)
        return self.damage_by_image.get(hashlib.sha256(jpeg_bytes).hexdigest(),
                                        {"image_assessable": True, "notes": "no damage", "damages": []})

    def status(self):
        return True, "scripted test provider"

    def analyze(self, jpeg_bytes):
        self.calls += 1
        if self.fail_with:
            raise VisionProviderError(self.fail_with)
        found = self.by_image.get(hashlib.sha256(jpeg_bytes).hexdigest(), self.default)
        if found is None:
            raise VisionProviderError("no scripted answer for this image")
        return dict(found)


def damage(kind="SCRATCH", part="REAR_BUMPER", severity="MODERATE", confidence=0.91, box=(0.2, 0.5, 0.45, 0.7),
           location=None):
    x0, y0, x1, y1 = box
    return {"damage_type": kind, "vehicle_part": part, "location": location or part.replace("_", " ").lower(),
            "severity": severity, "confidence": confidence,
            "box": {"x_min": x0, "y_min": y0, "x_max": x1, "y_max": y1}, "reason": f"visible {kind.lower()}"}
