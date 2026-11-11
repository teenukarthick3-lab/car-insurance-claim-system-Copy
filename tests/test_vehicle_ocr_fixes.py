import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
config.CHASSIS_OCR_FALLBACK = "off"
from app import create_app
from services import plate_ocr_service, chassis_ocr_service, policy_service, claim_service
from services import vehicle_image_validator as v, vision_providers, damage_detection_service
from tests.helpers import ScriptedProvider, make_photo, obs

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000


class TestVehicleOcrFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._mode = config.PHOTO_VALIDATION_MODE
        config.PHOTO_VALIDATION_MODE = "required"
        v.clear_cache()
        damage_detection_service.clear_cache()
        self.provider = ScriptedProvider()
        vision_providers.set_provider(self.provider)
        self.app = create_app({
            "DATABASE": os.path.join(self.tmp, "t.db"),
            "UPLOAD_FOLDER": self.tmp,
            "DAMAGE_ANALYSIS_SYNC": True
        })
        self.client = self.app.test_client()

        self.photos = {
            "front_photo": make_photo(seed=201),
            "back_photo": make_photo(seed=202),
            "left_photo": make_photo(seed=203),
            "right_photo": make_photo(seed=204),
            "chassis_photo": make_photo(seed=205),
        }
        self.provider.register(self.photos["front_photo"], obs("front", license_plate_text="TN38CT5038"))
        self.provider.register(self.photos["back_photo"], obs("rear", license_plate_text="TN38CT5038"))
        self.provider.register(self.photos["left_photo"], obs("left"))
        self.provider.register(self.photos["right_photo"], obs("right"))
        self.provider.register(self.photos["chassis_photo"], obs(
            "chassis", plate_text_lines=["MARUTI SUZUKI", "MA3JMT31SXG181197", "ENGINE NO. K10BN123456"],
            vin_text="MA3JMT31SXG181197"
        ))

        html = self.client.get("/claim").get_data(as_text=True)
        self.csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        self.client.post("/claim/verify", data={"csrf_token": self.csrf, "policy_number": "POL10001"})
        self.client.post("/claim/confirm-vehicle", data={"csrf_token": self.csrf, "vehicle_number": "TN38CT5038"})

    def tearDown(self):
        config.PHOTO_VALIDATION_MODE = self._mode
        vision_providers.set_provider(None)
        v.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _login_admin(self):
        a = self.app.test_client()
        html = a.get("/admin/login").get_data(as_text=True)
        t = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        a.post("/admin/login", data={"csrf_token": t, "username": "admin", "password": "Admin@123"})
        return a

    # 1. Validation tests
    def test_indian_registration_validation(self):
        # Valid Indian registration numbers
        self.assertTrue(policy_service.is_valid_registration("TN38CT5038"))
        self.assertTrue(policy_service.is_valid_registration("KA05MN3456"))
        self.assertTrue(policy_service.is_valid_registration("DL1CAB1234"))
        self.assertTrue(policy_service.is_valid_registration("MH12DE1433"))
        self.assertTrue(policy_service.is_valid_registration("DL01A1"))
        self.assertTrue(policy_service.is_valid_registration("22BH1234AA"))
        self.assertTrue(policy_service.is_valid_registration("TN385038"))

        # Invalid registration outputs (specifically MM24, partial reads, and noise)
        self.assertFalse(policy_service.is_valid_registration("MM24"))
        self.assertFalse(policy_service.is_valid_registration("MM 24"))
        self.assertFalse(policy_service.is_valid_registration("MM"))
        self.assertFalse(policy_service.is_valid_registration("1234"))
        self.assertFalse(policy_service.is_valid_registration("CAR"))
        self.assertFalse(policy_service.is_valid_registration("IND"))
        self.assertFalse(policy_service.is_valid_registration(""))
        self.assertFalse(policy_service.is_valid_registration(None))

    def test_chassis_validation_17_chars_no_ioq(self):
        # Valid: 17 characters, no I, O, Q
        self.assertTrue(chassis_ocr_service.is_valid_vin("MA3JMT31SXG181197"))
        self.assertTrue(chassis_ocr_service.is_valid_vin("1HGCR2F83HA000000"))

        # Invalid: contains I, O, or Q
        self.assertFalse(chassis_ocr_service.is_valid_vin("MA3JMT31IXG181197"))
        self.assertFalse(chassis_ocr_service.is_valid_vin("MA3JMT31OXG181197"))
        self.assertFalse(chassis_ocr_service.is_valid_vin("MA3JMT31QXG181197"))

        # Invalid: wrong length
        self.assertFalse(chassis_ocr_service.is_valid_vin("MA3JMT31S"))
        self.assertFalse(chassis_ocr_service.is_valid_vin("MA3JMT31SXG181197000"))
        self.assertFalse(chassis_ocr_service.is_valid_vin(""))
        self.assertFalse(chassis_ocr_service.is_valid_vin(None))

    # 2. Test Clear plate photo (valid read)
    def test_clear_plate_photo_valid_read(self):
        data = {
            "csrf_token": self.csrf,
            "damage_video": (io.BytesIO(VIDEO), "v.mp4"),
            "front_photo": (io.BytesIO(self.photos["front_photo"]), "front.jpg"),
            "back_photo": (io.BytesIO(self.photos["back_photo"]), "back.jpg"),
            "left_photo": (io.BytesIO(self.photos["left_photo"]), "left.jpg"),
            "right_photo": (io.BytesIO(self.photos["right_photo"]), "right.jpg"),
            "chassis_photo": (io.BytesIO(self.photos["chassis_photo"]), "chassis.jpg"),
        }
        res = self.client.post("/claim/submit", data=data, headers={"X-Requested-With": "fetch"},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)

        admin = self._login_admin()
        page = admin.get("/admin/claim/1").get_data(as_text=True)
        # Should show OCR: TN38CT5038 ✓ and NOT show Mismatch
        self.assertIn("OCR: TN38CT5038 ✓", page)
        self.assertNotIn("OCR: TN38CT5038 (Mismatch)", page)
        self.assertIn("Read from photo: TN38CT5038 · ✓ Verified", page)

    # 3. Test Blurry or irrelevant photo (shows manual review, NOT a mismatch, never displays raw invalid text)
    def test_blurry_or_invalid_ocr_shows_manual_review_not_mismatch(self):
        blurry_front = make_photo(seed=301)
        # OCR model produces invalid noise like MM24
        self.provider.register(blurry_front, obs("front", license_plate_text="MM24", plate_text_lines=["MM24"]))

        data = {
            "csrf_token": self.csrf,
            "damage_video": (io.BytesIO(VIDEO), "v.mp4"),
            "front_photo": (io.BytesIO(blurry_front), "front.jpg"),
            "back_photo": (io.BytesIO(self.photos["back_photo"]), "back.jpg"),
            "left_photo": (io.BytesIO(self.photos["left_photo"]), "left.jpg"),
            "right_photo": (io.BytesIO(self.photos["right_photo"]), "right.jpg"),
            "chassis_photo": (io.BytesIO(self.photos["chassis_photo"]), "chassis.jpg"),
        }
        res = self.client.post("/claim/submit", data=data, headers={"X-Requested-With": "fetch"},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)

        admin = self._login_admin()
        page = admin.get("/admin/claim/1").get_data(as_text=True)

        # Must show "OCR uncertain - manual review required"
        self.assertIn("OCR uncertain - manual review required", page)
        # Must NEVER show MM24 as OCR output or as a mismatch
        self.assertNotIn("MM24", page)
        self.assertNotIn("Mismatch", page)

    # 4. Test Clear plate differing from policy (shows a true mismatch)
    def test_clear_plate_differs_from_policy_shows_true_mismatch(self):
        diff_front = make_photo(seed=302)
        # OCR reads a valid registration number KA05MN3456 that differs from policy TN38CT5038
        self.provider.register(diff_front, obs("front", license_plate_text="KA05MN3456"))

        data = {
            "csrf_token": self.csrf,
            "damage_video": (io.BytesIO(VIDEO), "v.mp4"),
            "front_photo": (io.BytesIO(diff_front), "front.jpg"),
            "back_photo": (io.BytesIO(self.photos["back_photo"]), "back.jpg"),
            "left_photo": (io.BytesIO(self.photos["left_photo"]), "left.jpg"),
            "right_photo": (io.BytesIO(self.photos["right_photo"]), "right.jpg"),
            "chassis_photo": (io.BytesIO(self.photos["chassis_photo"]), "chassis.jpg"),
        }
        res = self.client.post("/claim/submit", data=data, headers={"X-Requested-With": "fetch"},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)

        admin = self._login_admin()
        page = admin.get("/admin/claim/1").get_data(as_text=True)

        # Must show true mismatch
        self.assertIn("OCR: KA05MN3456 (Mismatch)", page)
        self.assertIn("Read from photo: KA05MN3456 · Mismatch (Manual review required)", page)
        # Policy vehicle number remains unchanged
        self.assertIn('<span class="plate">TN38CT5038</span>', page)

    # 5. Test existing database record with invalid OCR like 'MM24' is treated as uncertain and not mismatch
    def test_legacy_database_with_invalid_ocr_displays_uncertain(self):
        db = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        db.execute("""INSERT INTO claims (id, claim_reference, policy_id, policy_number, customer_name,
                                         vehicle_number, ocr_vehicle_number, ocr_vehicle_status, status, submitted_at)
                      VALUES (99, 'CLM-TEST-INVALID', 1, 'POL10001', 'Test Owner', 'TN38CT5038', 'MM24', 'MANUAL_REVIEW',
                              'Pending Review', '2026-01-01T00:00:00')""")
        db.commit()
        db.close()

        admin = self._login_admin()
        page = admin.get("/admin/claim/99").get_data(as_text=True)

        # Must NOT show MM24 and must NOT show Mismatch
        self.assertNotIn("OCR: MM24 (Mismatch)", page)
        self.assertNotIn("MM24", page)
        self.assertIn("OCR uncertain - manual review required", page)

    # 6. Test chassis validation failure shows Manual review required
    def test_chassis_validation_failure_shows_manual_review_required(self):
        # AI returns invalid chassis (e.g. only 8 characters)
        bad_chassis_photo = make_photo(seed=303)
        self.provider.register(bad_chassis_photo, obs(
            "chassis", plate_text_lines=["BAD VIN", "MA3JMT31"], vin_text="MA3JMT31"
        ))

        data = {
            "csrf_token": self.csrf,
            "damage_video": (io.BytesIO(VIDEO), "v.mp4"),
            "front_photo": (io.BytesIO(self.photos["front_photo"]), "front.jpg"),
            "back_photo": (io.BytesIO(self.photos["back_photo"]), "back.jpg"),
            "left_photo": (io.BytesIO(self.photos["left_photo"]), "left.jpg"),
            "right_photo": (io.BytesIO(self.photos["right_photo"]), "right.jpg"),
            "chassis_photo": (io.BytesIO(bad_chassis_photo), "chassis.jpg"),
        }
        res = self.client.post("/claim/submit", data=data, headers={"X-Requested-With": "fetch"},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)

        admin = self._login_admin()
        page = admin.get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("Manual review required", page)
        self.assertNotIn('<span class="chassis-no">MA3JMT31</span>', page)


if __name__ == "__main__":
    unittest.main()
