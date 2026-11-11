"""End-to-end tests of the photo check inside the real Flask app (scripted vision model, temp DB)."""
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
config.CHASSIS_OCR_FALLBACK = "off"   # keep tests fast; OCR has its own tests
from app import create_app  # noqa: E402
from database import models  # noqa: E402
from services import vehicle_image_validator as v, vision_providers  # noqa: E402
from tests.helpers import ScriptedProvider, make_photo, obs  # noqa: E402

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000
CORRECT = {"front_photo": "front", "back_photo": "rear", "left_photo": "left",
           "right_photo": "right", "chassis_photo": "chassis"}


class ClaimFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._mode = config.PHOTO_VALIDATION_MODE
        config.PHOTO_VALIDATION_MODE = "required"
        v.clear_cache()
        self.provider = ScriptedProvider()
        vision_providers.set_provider(self.provider)
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "UPLOAD_FOLDER": self.tmp,
                                "DAMAGE_ANALYSIS_SYNC": True})
        self.client = self.app.test_client()
        self.photos = {}
        for i, (slot, kind) in enumerate(CORRECT.items()):
            self.photos[slot] = make_photo(seed=200 + i)
            self.provider.register(self.photos[slot], obs(kind))
        self.rear = make_photo(seed=300)
        self.provider.register(self.rear, obs("rear", confidence=0.97))
        self.csrf = self._verify("POL10001")

    def tearDown(self):
        config.PHOTO_VALIDATION_MODE = self._mode
        vision_providers.set_provider(None)
        v.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers -----------------------------------------------------------------
    def _verify(self, policy):
        html = self.client.get("/claim").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        self.client.post("/claim/verify", data={"csrf_token": token, "policy_number": policy})
        self.client.post("/claim/confirm-vehicle", data={"csrf_token": token, "vehicle_number": "TN 38 CT 5038"})
        return token

    def _check(self, slot, data, name="photo.jpg"):
        return self.client.post("/claim/validate-photo", headers={"X-Requested-With": "fetch"},
                                data={"csrf_token": self.csrf, "slot": slot, "photo": (io.BytesIO(data), name)},
                                content_type="multipart/form-data")

    def _submit(self, overrides=None):
        files = dict(self.photos, **(overrides or {}))
        data = {"csrf_token": self.csrf, "damage_video": (io.BytesIO(VIDEO), "v.mp4")}
        for slot, content in files.items():
            data[slot] = (io.BytesIO(content), f"{slot}.jpg")
        return self.client.post("/claim/submit", headers={"X-Requested-With": "fetch"}, data=data,
                                content_type="multipart/form-data")

    def _claims(self):
        con = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        con.row_factory = sqlite3.Row
        return con

    # tests ---------------------------------------------------------------------
    def test_validate_endpoint_accepts_and_rejects(self):
        ok = self._check("front_photo", self.photos["front_photo"]).get_json()
        self.assertTrue(ok["valid"])
        self.assertEqual(ok["title"], "Front view verified")
        bad = self._check("front_photo", self.rear).get_json()
        self.assertFalse(bad["valid"])
        self.assertIn("rear of the vehicle", bad["message"])
        for hidden in ("confidence", "details", "detected_view"):   # no technical info to customers
            self.assertNotIn(hidden, bad)

    def test_validate_requires_verified_policy(self):
        fresh = self.app.test_client()
        html = fresh.get("/claim").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        r = fresh.post("/claim/validate-photo", data={"csrf_token": token, "slot": "front_photo",
                                                      "photo": (io.BytesIO(self.rear), "x.jpg")},
                       content_type="multipart/form-data")
        self.assertEqual(r.status_code, 401)

    def test_validate_rejects_non_image(self):
        r = self._check("front_photo", b"%PDF-1.4 not a photo", "front.jpg").get_json()
        self.assertFalse(r["valid"])

    def test_good_claim_stores_validation_metadata(self):
        r = self._submit()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        con = self._claims()
        rows = {row["file_type"]: row for row in con.execute("SELECT * FROM claim_files")}
        self.assertEqual(rows["front_photo"]["expected_view"], "FRONT")
        self.assertEqual(rows["front_photo"]["detected_view"], "FRONT")
        self.assertEqual(rows["front_photo"]["validation_status"], "ACCEPTED")
        self.assertAlmostEqual(rows["front_photo"]["confidence"], 0.94)
        self.assertIn("MA3EWDE1S00123456", rows["chassis_photo"]["validation_details"])
        self.assertIsNone(rows["damage_video"]["validation_status"])

    def test_bypassing_the_browser_check_is_blocked(self):
        # Never called /claim/validate-photo; sends a rear view as the front photo directly.
        r = self._submit({"front_photo": self.rear})
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["rejected"][0]["slot"], "front_photo")
        self.assertIn("rear of the vehicle", body["error"])
        self.assertEqual(self._claims().execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)

    def test_swapped_sides_blocked_at_submit(self):
        r = self._submit({"left_photo": self.photos["right_photo"], "right_photo": self.photos["left_photo"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual({x["slot"] for x in r.get_json()["rejected"]}, {"left_photo", "right_photo"})

    def test_ai_down_at_submit_blocks_claim(self):
        v.clear_cache()
        self.provider.fail_with = "network down"
        r = self._submit()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self._claims().execute("SELECT COUNT(*) FROM claims").fetchone()[0], 0)

    def test_submit_reuses_earlier_checks(self):
        for slot, data in self.photos.items():
            self._check(slot, data)
        calls = self.provider.calls
        self.assertEqual(self._submit().status_code, 200)
        self.assertEqual(self.provider.calls, calls)     # re-checked from cache, no extra AI calls

    def test_rate_limit(self):
        limit = config.MAX_PHOTO_CHECKS_PER_SESSION
        try:
            config.MAX_PHOTO_CHECKS_PER_SESSION = 3
            codes = [self._check("front_photo", self.photos["front_photo"]).status_code for _ in range(4)]
        finally:
            config.MAX_PHOTO_CHECKS_PER_SESSION = limit
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_off_mode_stores_not_verified(self):
        config.PHOTO_VALIDATION_MODE = "off"
        self.assertEqual(self._submit({"front_photo": self.rear}).status_code, 200)
        statuses = {r[0] for r in self._claims().execute(
            "SELECT validation_status FROM claim_files WHERE file_type != 'damage_video'")}
        self.assertEqual(statuses, {"NOT_VERIFIED"})

    def test_admin_sees_ai_check(self):
        self.assertEqual(self._submit().status_code, 200)
        admin = self.app.test_client()
        html = admin.get("/admin/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        admin.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "Admin@123"})
        page = admin.get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("AI check: expected Front", page)
        self.assertIn("detected Left side", page)
        self.assertIn("MA3EWDE1S00123456", page)
        self.assertIn("AI photo check", admin.get("/admin/settings").get_data(as_text=True))

    def test_legacy_rear_photo_displays_in_admin(self):
        # Create a claim with a legacy 'rear_photo' record
        con = self._claims()
        con.execute("INSERT INTO claims (id, claim_reference, policy_id, policy_number, customer_name, vehicle_number, status, submitted_at) VALUES (99, 'CLM-OLD-01', 1, 'POL10001', 'Legacy User', 'TN38CT5038', 'Pending Review', '2026-01-01T00:00:00')")
        con.execute("INSERT INTO claim_files (claim_id, file_type, file_path, original_filename, file_size) VALUES (99, 'rear_photo', 'claims/CLM-OLD-01/rear.jpg', 'rear.jpg', 12345)")
        con.commit()
        admin = self.app.test_client()
        html = admin.get("/admin/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        admin.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "Admin@123"})
        page = admin.get("/admin/claim/99").get_data(as_text=True)
        self.assertIn("Rear View (previous)", page)


class Migration(unittest.TestCase):
    def test_old_database_gets_new_columns(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "old.db")
            con = sqlite3.connect(path)
            con.execute("""CREATE TABLE claim_files (id INTEGER PRIMARY KEY, claim_id INTEGER, file_type TEXT,
                           file_path TEXT, original_filename TEXT, mime_type TEXT, file_size INTEGER,
                           uploaded_at TEXT)""")
            con.execute("INSERT INTO claim_files (claim_id, file_type, file_path) VALUES (1, 'rear_photo', 'x')")
            con.commit()
            con.close()
            models.init_db(path)
            con = sqlite3.connect(path)
            cols = {r[1] for r in con.execute("PRAGMA table_info(claim_files)")}
            self.assertTrue({"expected_view", "detected_view", "confidence", "validated_at",
                             "validation_status", "validation_details"} <= cols)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM claim_files").fetchone()[0], 1)
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
