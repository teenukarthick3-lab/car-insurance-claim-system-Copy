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
from database import models
from services import file_service, plate_ocr_service, chassis_ocr_service, damage_detection_service, policy_service
from services import vehicle_image_validator as v, vision_providers
from tests.helpers import ScriptedProvider, make_photo, obs, damage

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000


class TestThreeChanges(unittest.TestCase):
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

        # Seed photos
        self.photos = {
            "front_photo": make_photo(seed=101),
            "back_photo": make_photo(seed=102),
            "left_photo": make_photo(seed=103),
            "right_photo": make_photo(seed=104),
            "chassis_photo": make_photo(seed=105),
        }
        self.provider.register(self.photos["front_photo"], obs("front", license_plate_text="TN 38 CT 5038"))
        self.provider.register(self.photos["back_photo"], obs("rear", license_plate_text="TN 38 CT 5038"))
        self.provider.register(self.photos["left_photo"], obs("left"))
        self.provider.register(self.photos["right_photo"], obs("right"))
        self.provider.register(self.photos["chassis_photo"], obs(
            "chassis", plate_text_lines=["MARUTI SUZUKI", "MA3JMT31SXG181197", "ENGINE NO. K10BN123456"],
            vin_text="MA3JMT31SXG181197"
        ))

        html = self.client.get("/claim").get_data(as_text=True)
        self.csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        self.client.post("/claim/verify", data={"csrf_token": self.csrf, "policy_number": "POL10001"})
        self.client.post("/claim/confirm-vehicle", data={"csrf_token": self.csrf, "vehicle_number": "TN 38 CT 5038"})

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

    # CHANGE 1 TESTS
    def test_change1_evidence_page_has_front_back_left_right(self):
        page = self.client.get("/claim/evidence").get_data(as_text=True)
        self.assertIn("Front view", page)
        self.assertIn("Back view", page)
        self.assertIn("Left side", page)
        self.assertIn("Right side", page)
        self.assertNotIn("Rear view", page)
        self.assertIn('data-key="front_photo"', page)
        self.assertNotIn('data-key="rear_photo"', page)

    def test_change1_legacy_rear_photo_backward_compatible(self):
        db = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        db.execute("""INSERT INTO claims (id, claim_reference, policy_id, policy_number, customer_name, vehicle_number, status, submitted_at)
                      VALUES (50, 'CLM-LEGACY', 1, 'POL10001', 'Legacy Owner', 'TN38CT5038', 'Pending Review', '2026-01-01T00:00:00')""")
        db.execute("""INSERT INTO claim_files (claim_id, file_type, file_path, original_filename, file_size)
                      VALUES (50, 'rear_photo', 'claims/CLM-LEGACY/rear.jpg', 'rear.jpg', 1234)""")
        db.commit()
        db.close()

        admin = self._login_admin()
        resp = admin.get("/admin/claim/50")
        self.assertEqual(resp.status_code, 200)
        content = resp.get_data(as_text=True)
        self.assertIn("Rear View (previous)", content)

    # CHANGE 2 TESTS
    def test_change2_plate_ocr_normalization_and_validation(self):
        # Normalization
        self.assertEqual(policy_service.normalize_registration("tn 38 ct 5038"), "TN38CT5038")
        self.assertEqual(policy_service.normalize_registration("TN-38-CT-5038"), "TN38CT5038")
        # Validation Indian formats
        self.assertTrue(policy_service.is_valid_registration("TN38CT5038"))
        self.assertTrue(policy_service.is_valid_registration("DL1CAB1234"))
        self.assertTrue(policy_service.is_valid_registration("MH02CB1234"))
        self.assertTrue(policy_service.is_valid_registration("22BH1234AA"))
        self.assertFalse(policy_service.is_valid_registration("INVALID_123"))

    def test_change2_ocr_extracted_and_displayed(self):
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
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))

        admin = self._login_admin()
        page = admin.get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("TN38CT5038", page)
        self.assertIn("MA3JMT31SXG181197", page)
        self.assertIn("Read from photo", page)
        self.assertIn("✓ Verified", page)

    def test_change2_ocr_mismatch_flagged_and_does_not_overwrite_policy(self):
        # Set front photo plate text to a different valid vehicle number
        diff_front = make_photo(seed=999)
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
        # Primary verified vehicle number remains TN38CT5038
        self.assertIn('<span class="plate">TN38CT5038</span>', page)
        # Secondary note flags mismatch for manual review
        self.assertIn("Mismatch (Manual review required)", page)
        self.assertIn("KA05MN3456", page)

    def test_change2_ocr_failure_shows_manual_review_required(self):
        no_ocr_front = make_photo(seed=888)
        self.provider.register(no_ocr_front, obs("front", license_plate_text=None))
        no_ocr_back = make_photo(seed=887)
        self.provider.register(no_ocr_back, obs("rear", license_plate_text=None))

        data = {
            "csrf_token": self.csrf,
            "damage_video": (io.BytesIO(VIDEO), "v.mp4"),
            "front_photo": (io.BytesIO(no_ocr_front), "front.jpg"),
            "back_photo": (io.BytesIO(no_ocr_back), "back.jpg"),
            "left_photo": (io.BytesIO(self.photos["left_photo"]), "left.jpg"),
            "right_photo": (io.BytesIO(self.photos["right_photo"]), "right.jpg"),
            "chassis_photo": (io.BytesIO(self.photos["chassis_photo"]), "chassis.jpg"),
        }
        res = self.client.post("/claim/submit", data=data, headers={"X-Requested-With": "fetch"},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)

        admin = self._login_admin()
        page = admin.get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("Read from photo: manual review required", page)

    # CHANGE 3 TESTS
    def test_change3_multiple_damage_photos_upload_and_admin_view(self):
        dmg1 = make_photo(seed=551)
        dmg2 = make_photo(seed=552)

        # Register vision damage detections
        self.provider.register_damage(dmg1, [damage("SCRATCH", "DOOR", "MINOR", 0.92)])
        self.provider.register_damage(dmg2, [damage("DENT", "BUMPER", "MODERATE", 0.88)])

        data = {
            "csrf_token": self.csrf,
            "damage_video": (io.BytesIO(VIDEO), "v.mp4"),
            "front_photo": (io.BytesIO(self.photos["front_photo"]), "front.jpg"),
            "back_photo": (io.BytesIO(self.photos["back_photo"]), "back.jpg"),
            "left_photo": (io.BytesIO(self.photos["left_photo"]), "left.jpg"),
            "right_photo": (io.BytesIO(self.photos["right_photo"]), "right.jpg"),
            "chassis_photo": (io.BytesIO(self.photos["chassis_photo"]), "chassis.jpg"),
            "damage_photos": [
                (io.BytesIO(dmg1), "scratch1.jpg"),
                (io.BytesIO(dmg2), "dent2.jpg"),
            ]
        }
        res = self.client.post("/claim/submit", data=data, headers={"X-Requested-With": "fetch"},
                               content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)

        db = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        db.row_factory = sqlite3.Row
        dmg_rows = db.execute("SELECT * FROM claim_damage_photos ORDER BY id").fetchall()
        self.assertEqual(len(dmg_rows), 2)
        self.assertEqual(dmg_rows[0]["damage_type"], "Scratch")
        self.assertAlmostEqual(dmg_rows[0]["confidence"], 0.92)
        self.assertEqual(dmg_rows[1]["damage_type"], "Dent")
        self.assertAlmostEqual(dmg_rows[1]["confidence"], 0.88)
        db.close()

        admin = self._login_admin()
        page = admin.get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("Damage Photos", page)
        self.assertIn("Damage Photo #1", page)
        self.assertIn("Damage Photo #2", page)
        self.assertIn("Detected: <b>Scratch</b>", page)
        self.assertIn("Detected: <b>Dent</b>", page)

    def test_change3_damage_photos_optional(self):
        # Submit without damage photos must succeed seamlessly
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


if __name__ == "__main__":
    unittest.main()
