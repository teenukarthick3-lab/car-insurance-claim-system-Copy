"""Unit tests for AI photo validation.  Run:  python -m unittest discover -s tests -v"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageEnhance, ImageFilter  # noqa: E402

import config  # noqa: E402
config.CHASSIS_OCR_FALLBACK = "off"   # keep tests fast; OCR has its own tests
from services import vehicle_image_validator as v  # noqa: E402
from services import vision_providers  # noqa: E402
from tests.helpers import ScriptedProvider, make_photo, obs  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = (config.PHOTO_VALIDATION_MODE, config.CONFIDENCE_THRESHOLD)
        config.PHOTO_VALIDATION_MODE = "required"
        config.CONFIDENCE_THRESHOLD = 0.80
        v.clear_cache()
        self.provider = ScriptedProvider()
        vision_providers.set_provider(self.provider)
        self.seed = 0

    def tearDown(self):
        config.PHOTO_VALIDATION_MODE, config.CONFIDENCE_THRESHOLD = self._saved
        vision_providers.set_provider(None)
        v.clear_cache()

    def check(self, slot, kind, **overrides):
        """Upload a fresh photo whose content the model 'sees' as `kind` into `slot`."""
        self.seed += 1
        data = make_photo(seed=self.seed)
        self.provider.register(data, obs(kind, **overrides))
        return v.validate_slot(slot, data)

    def assertAccepted(self, r):
        self.assertTrue(r["valid"], r)
        self.assertEqual(r["status"], "ACCEPTED")

    def assertRejected(self, r, code=None):
        self.assertFalse(r["valid"], r)
        if code:
            self.assertEqual(r["code"], code, r)


class FrontSlot(Base):
    def test_front_image_accepted(self):
        r = self.check("front_photo", "front")
        self.assertAccepted(r)
        self.assertEqual((r["expected_view"], r["detected_view"]), ("FRONT", "FRONT"))
        self.assertEqual(r["title"], "Front view verified")
        self.assertAlmostEqual(r["confidence"], 0.94)

    def test_three_quarter_front_accepted(self):
        self.assertAccepted(self.check("front_photo", "front_three_quarter"))

    def test_rear_image_rejected_with_rear_message(self):
        r = self.check("front_photo", "rear", confidence=0.96)
        self.assertRejected(r, "WRONG_VIEW")
        self.assertEqual(r["detected_view"], "REAR")
        self.assertEqual(r["title"], "Incorrect vehicle angle")
        self.assertIn("appears to show the rear of the vehicle", r["message"])
        self.assertIn("Please upload the front view", r["message"])

    def test_left_and_right_images_rejected(self):
        self.assertRejected(self.check("front_photo", "left"), "WRONG_VIEW")
        self.assertRejected(self.check("front_photo", "right"), "WRONG_VIEW")

    def test_random_image_rejected(self):
        r = self.check("front_photo", "random", confidence=0.98)
        self.assertRejected(r, "NO_VEHICLE")
        self.assertEqual(r["detected_view"], "UNKNOWN")

    def test_chassis_closeup_rejected(self):
        self.assertRejected(self.check("front_photo", "chassis"), "WRONG_IMAGE")

    def test_interior_rejected(self):
        self.assertRejected(self.check("front_photo", "interior"), "WRONG_IMAGE")

    def test_model_says_poor_quality(self):
        r = self.check("front_photo", "poor")
        self.assertRejected(r, "LOW_QUALITY")
        self.assertIn("clearer image", r["message"])

    def test_uncertain_front_not_accepted(self):
        r = self.check("front_photo", "front", confidence=0.55)
        self.assertRejected(r, "LOW_CONFIDENCE")
        self.assertEqual(r["title"], "Unable to confidently verify this image")

    def test_threshold_is_configurable(self):
        config.CONFIDENCE_THRESHOLD = 0.5
        self.assertAccepted(self.check("front_photo", "front", confidence=0.55))
        config.CONFIDENCE_THRESHOLD = 0.99
        self.assertRejected(self.check("front_photo", "front", confidence=0.95), "LOW_CONFIDENCE")


class BackSlot(Base):
    def test_rear_accepted_front_and_side_rejected(self):
        r = self.check("back_photo", "rear")
        self.assertAccepted(r)
        self.assertEqual(r["title"], "Back view verified")
        front = self.check("back_photo", "front")
        self.assertRejected(front, "WRONG_VIEW")
        self.assertIn("back view", front["message"])
        self.assertRejected(self.check("back_photo", "left"), "WRONG_VIEW")


class SideSlots(Base):
    def test_left(self):
        self.assertAccepted(self.check("left_photo", "left"))
        wrong_side = self.check("left_photo", "right")
        self.assertRejected(wrong_side, "WRONG_VIEW")
        self.assertEqual(wrong_side["title"], "Incorrect side view")
        self.assertIn("showing the LEFT side", wrong_side["message"])
        self.assertRejected(self.check("left_photo", "front"), "WRONG_VIEW")
        self.assertRejected(self.check("left_photo", "rear"), "WRONG_VIEW")

    def test_right(self):
        self.assertAccepted(self.check("right_photo", "right"))
        wrong_side = self.check("right_photo", "left")
        self.assertRejected(wrong_side, "WRONG_VIEW")
        self.assertIn("showing the RIGHT side", wrong_side["message"])
        self.assertRejected(self.check("right_photo", "front"), "WRONG_VIEW")
        self.assertRejected(self.check("right_photo", "rear"), "WRONG_VIEW")

    def test_side_with_unclear_direction_is_not_guessed(self):
        self.assertRejected(self.check("left_photo", "side_unclear"), "LOW_CONFIDENCE")

    def test_side_is_derived_from_nose_direction(self):
        # Standing at the vehicle's LEFT side, its front points to the left of the picture.
        self.assertEqual(v.detect_view(vision_providers.normalize_observation(obs("left"))), "LEFT_SIDE")
        self.assertEqual(v.detect_view(vision_providers.normalize_observation(obs("right"))), "RIGHT_SIDE")


class ChassisSlot(Base):
    def test_clear_vin_accepted_and_text_kept(self):
        r = self.check("chassis_photo", "chassis")
        self.assertAccepted(r)
        self.assertEqual(r["detected_view"], "CHASSIS")
        self.assertEqual(r["details"]["vin_text"], "MA3EWDE1S00123456")

    def test_partially_read_vin_still_accepted(self):
        # OCR not reading every character is not a reason to reject on its own.
        self.assertAccepted(self.check("chassis_photo", "chassis_partial"))

    def test_normal_vehicle_photo_rejected(self):
        r = self.check("chassis_photo", "rear")
        self.assertRejected(r, "WRONG_IMAGE")
        self.assertIn("does not appear to contain a chassis/VIN number", r["message"])

    def test_random_image_rejected(self):
        self.assertRejected(self.check("chassis_photo", "random"), "WRONG_IMAGE")

    def test_unreadable_asks_for_clearer(self):
        r = self.check("chassis_photo", "chassis_illegible")
        self.assertRejected(r, "UNREADABLE")
        self.assertIn("closer", r["message"])

    def test_uncertain_asks_for_clearer(self):
        self.assertRejected(self.check("chassis_photo", "chassis", confidence=0.4), "LOW_CONFIDENCE")


class LocalQuality(Base):
    def _img_bytes(self, img):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    def test_blurry_dark_black_tiny_rejected_without_calling_ai(self):
        sharp = Image.open(io.BytesIO(make_photo(seed=99)))
        cases = {
            "blurry": sharp.filter(ImageFilter.GaussianBlur(12)),
            "dark": ImageEnhance.Brightness(sharp).enhance(0.12),
            "black": Image.new("RGB", (1280, 960), (2, 2, 2)),
            "tiny": sharp.resize((320, 240)),
        }
        for name, img in cases.items():
            with self.subTest(name):
                r = v.validate_slot("front_photo", self._img_bytes(img))
                self.assertRejected(r, "LOW_QUALITY")
                self.assertIn("Please upload a clearer image", r["message"])
        self.assertEqual(self.provider.calls, 0)

    def test_not_an_image(self):
        self.assertRejected(v.validate_slot("front_photo", b"not an image at all"), "LOW_QUALITY")

    def test_exif_rotation_applied_before_analysis(self):
        img = v._open_image(make_photo(seed=5, size=(1280, 960), exif_orientation=6))
        self.assertEqual(img.size, (960, 1280))


class Behaviour(Base):
    def test_filename_is_ignored(self):
        data = make_photo(seed=42)
        self.provider.register(data, obs("front"))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "front.jpg")
            with open(path, "wb") as fh:
                fh.write(data)
            r = v.validate_vehicle_image(path, "FRONT")
        self.assertAccepted(r)
        self.assertEqual(r["detected_view"], "FRONT")

    def test_model_is_not_told_the_expected_view(self):
        seen = []
        p = ScriptedProvider(default=obs("rear"))
        p.analyze = lambda jpeg: seen.append(jpeg) or obs("rear")
        vision_providers.set_provider(p)
        v.validate_slot("left_photo", make_photo(seed=8))
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], bytes)   # image only, no slot/expected view

    def test_same_photo_is_analyzed_once(self):
        data = make_photo(seed=7)
        self.provider.register(data, obs("front"))
        v.validate_slot("front_photo", data)
        v.validate_slot("front_photo", data)
        v.validate_slot("back_photo", data)
        self.assertEqual(self.provider.calls, 1)

    def test_provider_failure_is_not_accepted(self):
        self.provider.fail_with = "timeout"
        r = v.validate_slot("front_photo", make_photo(seed=3))
        self.assertRejected(r, "UNAVAILABLE")
        self.assertTrue(r["retryable"])
        self.assertEqual(r["status"], "ERROR")

    def test_off_mode_never_claims_verified(self):
        config.PHOTO_VALIDATION_MODE = "off"
        r = v.validate_slot("front_photo", make_photo(seed=4))
        self.assertTrue(r["valid"])
        self.assertEqual(r["status"], "NOT_VERIFIED")
        self.assertNotIn("verified", r["title"].lower())
        self.assertEqual(self.provider.calls, 0)

    def test_auto_mode_without_ai_accepts_as_not_verified(self):
        config.PHOTO_VALIDATION_MODE = "auto"
        vision_providers.set_provider(vision_providers.GeminiVisionProvider(api_key=""))
        r = v.validate_slot("front_photo", make_photo(seed=11))
        self.assertTrue(r["valid"])
        self.assertEqual(r["status"], "NOT_VERIFIED")
        self.assertIn("GEMINI_API_KEY", r["details"]["ai"])
        self.assertFalse(v.validation_status()["active"])

    def test_auto_mode_still_rejects_low_quality_without_ai(self):
        config.PHOTO_VALIDATION_MODE = "auto"
        vision_providers.set_provider(vision_providers.GeminiVisionProvider(api_key=""))
        black = io.BytesIO()
        Image.new("RGB", (1280, 960), (1, 1, 1)).save(black, format="JPEG")
        self.assertEqual(v.validate_slot("front_photo", black.getvalue())["code"], "LOW_QUALITY")

    def test_auto_mode_uses_ai_when_configured(self):
        config.PHOTO_VALIDATION_MODE = "auto"
        self.assertRejected(self.check("front_photo", "rear"), "WRONG_VIEW")

    def test_auto_mode_fails_closed_when_configured_ai_breaks(self):
        config.PHOTO_VALIDATION_MODE = "auto"
        self.provider.fail_with = "credit balance too low"
        self.assertRejected(v.validate_slot("front_photo", make_photo(seed=12)), "UNAVAILABLE")

    def test_required_mode_without_ai_blocks(self):
        vision_providers.set_provider(vision_providers.GeminiVisionProvider(api_key=""))
        self.assertRejected(v.validate_slot("front_photo", make_photo(seed=13)), "UNAVAILABLE")

    def test_parallel_validation(self):
        photos = {}
        for i, (slot, kind) in enumerate([("front_photo", "front"), ("back_photo", "rear"), ("left_photo", "left"),
                                          ("right_photo", "right"), ("chassis_photo", "chassis")]):
            photos[slot] = make_photo(seed=100 + i)
            self.provider.register(photos[slot], obs(kind))
        results = v.validate_slots(photos)
        self.assertTrue(all(r["valid"] for r in results.values()), results)

    def test_structured_result_shape(self):
        r = self.check("front_photo", "front")
        for key in ("valid", "expected_view", "detected_view", "confidence", "message", "status", "validated_at"):
            self.assertIn(key, r)


class Normalization(unittest.TestCase):
    def test_messy_model_output_is_made_safe(self):
        n = vision_providers.normalize_observation({
            "photo_type": "vehicle exterior", "vehicle_present": True, "dominant_view": "rear",
            "nose_direction": "away-from-camera", "confidence": 93, "quality_issues": ["blurry", "nonsense"],
            "vin_text": "null"})
        self.assertEqual(n["photo_type"], "VEHICLE_EXTERIOR")
        self.assertEqual(n["dominant_view"], "REAR")
        self.assertEqual(n["nose_direction"], "AWAY_FROM_CAMERA")
        self.assertAlmostEqual(n["confidence"], 0.93)
        self.assertEqual(n["quality_issues"], ["blurry"])
        self.assertIsNone(n["vin_text"])

    def test_garbage_raises(self):
        with self.assertRaises(vision_providers.VisionProviderError):
            vision_providers.normalize_observation("not a dict")


try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None


@unittest.skipIf(genai is None, "google-genai package not installed")
class GeminiProviderWiring(unittest.TestCase):
    """Runs the Gemini provider against a mock client (no network, no API key needed)."""

    def _provider(self, fake_generate):
        class FakeModels:
            def generate_content(self, *args, **kwargs):
                return fake_generate(*args, **kwargs)

        class FakeClient:
            models = FakeModels()

        return vision_providers.GeminiVisionProvider(api_key="test-key", model="gemini-3.5-flash-lite", client=FakeClient())

    def test_request_and_response(self):
        captured = {}

        def handler(*args, **kwargs):
            captured["kwargs"] = kwargs
            reply = obs("rear")
            class FakeResponse:
                text = json.dumps(reply)
            return FakeResponse()

        result = self._provider(handler).analyze(make_photo(seed=1))
        kwargs = captured["kwargs"]
        self.assertEqual(kwargs["model"], "gemini-3.5-flash-lite")
        config_obj = kwargs["config"]
        self.assertEqual(config_obj.response_mime_type, "application/json")
        self.assertEqual(config_obj.response_schema["required"],
                         vision_providers.OBSERVATION_SCHEMA["required"])
        part = kwargs["contents"][0]
        self.assertEqual(part.inline_data.mime_type, "image/jpeg")
        prompt = kwargs["contents"][1]
        for leak in ("rear_photo", "expected"):
            self.assertNotIn(leak, prompt.lower())
        self.assertEqual(result["dominant_view"], "REAR")

    def test_http_error_becomes_provider_error(self):
        def handler(*args, **kwargs):
            raise RuntimeError("busy")
        p = self._provider(handler)
        with self.assertRaises(vision_providers.VisionProviderError):
            p.analyze(make_photo(seed=2))

    def test_unparseable_reply_becomes_provider_error(self):
        def handler(*args, **kwargs):
            class FakeResponse:
                text = "Sorry, I can't help"
            return FakeResponse()
        with self.assertRaises(vision_providers.VisionProviderError):
            self._provider(handler).analyze(make_photo(seed=3))

    def test_missing_key_reported(self):
        ready, why = vision_providers.GeminiVisionProvider(api_key="").status()
        self.assertFalse(ready)
        self.assertIn("GEMINI_API_KEY", why)


if __name__ == "__main__":
    unittest.main()
