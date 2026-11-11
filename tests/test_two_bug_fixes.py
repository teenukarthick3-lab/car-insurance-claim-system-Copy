"""Tests for the two bug fixes:
Bug 1: Full damage detection, box resilience, unknown type/severity mapping, retry on inconsistent notes, and repair estimation.
Bug 2: Vehicle number strictly from photo OCR, no fallback to policy vehicle number, no Mismatch text.
"""
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
from database.seed import seed
from services import (
    claim_service,
    damage_detection_service as dds,
    estimation_service as est,
    policy_service,
    vehicle_image_validator as v,
    vision_providers,
)
from services.vision_providers import normalize_damage_report, notes_describe_damage
from tests.helpers import ScriptedProvider, make_photo, obs

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000
CORRECT = {
    "front_photo": "front",
    "back_photo": "rear",
    "left_photo": "left",
    "right_photo": "right",
    "chassis_photo": "chassis",
}


_DEFAULT_BOX = {"x_min": 0.2, "y_min": 0.3, "x_max": 0.5, "y_max": 0.6}


def make_damage(dtype, part, sev="minor", conf=0.92, reason="visible damage", box=_DEFAULT_BOX):
    d = {
        "damage_type": dtype,
        "vehicle_part": part,
        "location": f"{part} area",
        "severity": sev,
        "reason": reason,
    }
    if conf is not None:
        d["confidence"] = conf
    if box is not False and box is not None:
        d["box"] = box
    return d


class TestTwoBugFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._mode = config.PHOTO_VALIDATION_MODE
        config.PHOTO_VALIDATION_MODE = "required"
        v.clear_cache()
        dds.clear_cache()

        self.provider = ScriptedProvider()
        vision_providers.set_provider(self.provider)

        self.db_path = os.path.join(self.tmp, "test.db")
        self.upload_dir = os.path.join(self.tmp, "uploads")
        os.makedirs(os.path.join(self.upload_dir, "claims"), exist_ok=True)

        models.init_db(self.db_path)
        seed(self.db_path)

        self.app = create_app({
            "DATABASE": self.db_path,
            "UPLOAD_FOLDER": self.upload_dir,
            "DAMAGE_ANALYSIS_SYNC": True,
            "TESTING": True,
            "SECRET_KEY": "test-key-fixed",
        })

        self.photos = {}
        for i, (slot, kind) in enumerate(CORRECT.items()):
            self.photos[slot] = make_photo(seed=100 + i)
            # Default OCR reads TN38CX3972
            plate_kw = {"license_plate_text": "TN38CX3972"} if slot in ("front_photo", "back_photo") else {}
            self.provider.register(self.photos[slot], obs(kind, **plate_kw))

        def fake_extract(storage, image_kind):
            if image_kind == "registration_plate":
                return getattr(self, "plate_ocr_mock", {"value": "TN38CX3972", "status": "format_valid", "raw_detected_text": "TN38CX3972"})
            elif image_kind == "chassis_plate":
                return {"value": "MA3EWDE1S00123456", "status": "format_valid", "raw_detected_text": "MA3EWDE1S00123456"}
            return {"value": None, "status": "not_detected"}

        from unittest.mock import patch
        self.ocr_patch = patch("routes.customer._extract_with_gemini", side_effect=fake_extract)
        self.ocr_patch.start()

    def tearDown(self):
        self.ocr_patch.stop()
        config.PHOTO_VALIDATION_MODE = self._mode
        vision_providers.set_provider(None)
        v.clear_cache()
        dds.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get_csrf(self, client, url="/claim/evidence"):
        html = client.get(url).get_data(as_text=True)
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        return m.group(1) if m else ""

    def _login_admin(self, client):
        csrf = self._get_csrf(client, "/admin/login")
        client.post("/admin/login", data={"csrf_token": csrf, "username": "admin", "password": "Admin@123"})

    # =========================================================================
    # BUG 1 TESTS
    # =========================================================================

    def test_bug1_missing_or_invalid_box_not_dropped(self):
        """Do not silently drop a damage entry because of an invalid or missing bounding box."""
        raw = {
            "image_assessable": True,
            "notes": "Damages found",
            "damages": [
                make_damage("dent", "bonnet", "minor", 0.90, box=None),  # no box
                make_damage("scratch", "front bumper", "minor", 0.91, box={"x_min": 0, "y_min": 0, "x_max": 0.001, "y_max": 0.001}),  # too small
                make_damage("crack", "front bumper", "moderate", 0.95, box=[0.1, 0.2, 0.4, 0.5]),  # list box
            ]
        }
        report = normalize_damage_report(raw)
        self.assertEqual(len(report["damages"]), 3, "All 3 damage entries must be kept, none dropped")
        self.assertEqual(report["damages"][0]["box"], {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0})
        self.assertEqual(report["damages"][1]["box"], {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0})
        self.assertGreater(report["damages"][2]["box"]["w"], 0.0)

    def test_bug1_unknown_damage_type_and_severity_mapped(self):
        """Map unknown damage_type values to 'other' and unknown severity to 'uncertain'."""
        raw = {
            "image_assessable": True,
            "notes": "Weird damages",
            "damages": [
                make_damage("UNKNOWN_MYSTERY_DAMAGE", "bonnet", "UNKNOWN_LEVEL", 0.88),
            ]
        }
        report = normalize_damage_report(raw)
        self.assertEqual(len(report["damages"]), 1)
        self.assertEqual(report["damages"][0]["damage_type"], "OTHER")
        self.assertEqual(report["damages"][0]["severity"], "UNCERTAIN")

    def test_bug1_notes_describe_damage_helper(self):
        """Test accuracy of notes_describe_damage."""
        self.assertTrue(notes_describe_damage("The vehicle has severe front-end damage, including a smashed front bumper, deformed bonnet, and damaged front fender."))
        self.assertTrue(notes_describe_damage("Severe frontal impact damage with crushed front bumper, deformed fender, and broken headlight."))
        self.assertTrue(notes_describe_damage("Noticeable dent on the door panel."))
        self.assertFalse(notes_describe_damage("The rear of the vehicle appears clean and undamaged."))
        self.assertFalse(notes_describe_damage("The vehicle's left side is visible, and no physical damage can be detected in this photo."))
        self.assertFalse(notes_describe_damage("The vehicle's right side appears free of visible damage."))
        self.assertFalse(notes_describe_damage("No visible damage detected."))

    def test_bug1_consistency_guard_shows_review_manually_never_false_no_damage(self):
        """If notes describe damage but list is empty, mark as inconsistent and show 'review manually', never 'No visible damage detected'."""
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        # Register photo with damage notes but empty damage list
        self.provider.register_damage(
            self.photos["front_photo"],
            damages=[],
            notes="The vehicle has severe front-end damage, including a smashed front bumper, deformed bonnet, and damaged front fender."
        )

        data = {
            "csrf_token": csrf,
            "policy_number": "POL10001",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)

        self._login_admin(client)
        page = client.get("/admin/claim/1").get_data(as_text=True)

        stage1 = re.search(r'id="da-1"[\s\S]*?(?=<div class="da-stage|<p class="ai-note"|</section>)', page).group(0)
        self.assertIn("AI result inconsistent — review manually", stage1)
        self.assertNotIn("No visible damage detected in this photo", stage1)

    def test_bug1_clean_photo_shows_no_visible_damage(self):
        """A clean car photo with no damage shows 'No visible damage detected' with no estimate."""
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        for slot, p_bytes in self.photos.items():
            self.provider.register_damage(p_bytes, damages=[], notes="The vehicle appears clean and undamaged.")

        data = {
            "csrf_token": csrf,
            "policy_number": "POL10001",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)

        self._login_admin(client)
        page = client.get("/admin/claim/1").get_data(as_text=True)

        self.assertIn("No visible damage detected in this photo", page)
        self.assertIn("Estimate unavailable", page)

    def test_bug1_damaged_front_photo_returns_entries_and_estimate(self):
        """Damaged front photo returns all entries (bumper, bonnet, fender) and repair estimate."""
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        self.provider.register_damage(
            self.photos["front_photo"],
            damages=[
                make_damage("bumper_damage", "front bumper", "severe", 0.95),
                make_damage("panel_deformation", "bonnet", "severe", 0.92),
                make_damage("panel_deformation", "left front fender", "moderate", 0.90),
                make_damage("lamp_damage", "left headlight", "severe", 0.94),
            ],
            notes="Severe frontal impact damage."
        )

        data = {
            "csrf_token": csrf,
            "policy_number": "POL10001",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)

        self._login_admin(client)
        page = client.get("/admin/claim/1").get_data(as_text=True)

        self.assertIn("Bumper damage", page)
        self.assertIn("Panel deformation", page)
        self.assertIn("Lamp damage", page)
        self.assertIn("Estimated total", page)

    # =========================================================================
    # BUG 2 TESTS
    # =========================================================================

    def test_bug2_vehicle_number_from_photo_ocr_only_no_policy_number_no_mismatch(self):
        """Vehicle number comes ONLY from photo OCR. Policy vehicle number (TN38CT5038) never appears in vehicle slot and no Mismatch text."""
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        # Policy POL10001 has stored vehicle number TN38CT5038
        # Photo OCR extracted TN38CX3972
        data = {
            "csrf_token": csrf,
            "policy_number": "POL10001",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)

        self._login_admin(client)
        page = client.get("/admin/claim/1").get_data(as_text=True)

        # 1. Header card VEHICLE slot shows photo-extracted TN38CX3972 with "Read from photo"
        self.assertIn("TN38CX3972", page)
        self.assertIn("Read from photo", page)

        # 2. No "(Mismatch)" text anywhere
        self.assertNotIn("Mismatch", page)
        self.assertNotIn("(Mismatch)", page)

        # 3. Policy vehicle number TN38CT5038 must NOT be in the Vehicle slot
        # Header vehicle block:
        m = re.search(r'<div class="fact">\s*<div class="k">Vehicle</div>([\s\S]*?)</div>\s*</div>', page)
        self.assertTrue(m, "Vehicle fact block must exist")
        fact_html = m.group(1)
        self.assertIn("TN38CX3972", fact_html)
        self.assertNotIn("TN38CT5038", fact_html)
        self.assertNotIn("Verified", fact_html)

    def test_bug2_blurry_plate_photo_shows_manual_review_required(self):
        """A blurry plate photo shows 'Manual review required - see vehicle photo' and never falls back to policy number."""
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        self.plate_ocr_mock = {"value": None, "status": "not_detected", "raw_detected_text": ""}
        for slot in ("front_photo", "back_photo"):
            self.provider.register(self.photos[slot], obs(CORRECT[slot], license_plate_text=None))

        data = {
            "csrf_token": csrf,
            "policy_number": "POL10001",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)

        self._login_admin(client)
        page = client.get("/admin/claim/1").get_data(as_text=True)

        self.assertIn("Manual review required - see vehicle photo", page)
        # Must never show policy vehicle number TN38CT5038 in vehicle slot
        m = re.search(r'<div class="fact">\s*<div class="k">Vehicle</div>([\s\S]*?)</div>\s*</div>', page)
        self.assertTrue(m)
        self.assertNotIn("TN38CT5038", m.group(1))

    def test_bug2_old_claims_display_ocr_number_or_manual_review(self):
        """Old claims in DB with vehicle_number saved as policy number must not show it."""
        conn = models.connect(self.db_path)
        try:
            # Simulate old claim where vehicle_number was saved as policy's 'TN38CT5038',
            # but ocr_vehicle_number was read as 'TN38CX3972'
            conn.execute(
                """INSERT INTO claims (claim_reference, policy_id, policy_number, customer_name,
                                       vehicle_number, ocr_vehicle_number, ocr_vehicle_status,
                                       submitted_at, status, created_at, updated_at)
                   VALUES ('CLM-2025-000099', 1, 'POL10001', 'Kavitha S.',
                           'TN38CT5038', 'TN38CX3972', 'VERIFIED',
                           '2025-10-01 10:00:00', 'Pending Review', '2025-10-01 10:00:00', '2025-10-01 10:00:00')"""
            )
            old_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        finally:
            conn.close()

        client = self.app.test_client()
        self._login_admin(client)
        page = client.get(f"/admin/claim/{old_id}").get_data(as_text=True)

        m = re.search(r'<div class="fact">\s*<div class="k">Vehicle</div>([\s\S]*?)</div>\s*</div>', page)
        self.assertTrue(m)
        fact_html = m.group(1)
        self.assertIn("TN38CX3972", fact_html)
        self.assertNotIn("TN38CT5038", fact_html)
        self.assertNotIn("Mismatch", page)

    def test_bug2_vehicle_search_matches_photo_ocr_only(self):
        """Vehicle search matches the photo-extracted number only."""
        conn = models.connect(self.db_path)
        try:
            conn.execute(
                """INSERT INTO claims (claim_reference, policy_id, policy_number, customer_name,
                                       vehicle_number, ocr_vehicle_number, ocr_vehicle_status,
                                       submitted_at, status, created_at, updated_at)
                   VALUES ('CLM-2025-000100', 1, 'POL10001', 'Test Customer',
                           'TN38CT5038', 'TN38CX3972', 'VERIFIED',
                           '2025-10-01 10:00:00', 'Pending Review', '2025-10-01 10:00:00', '2025-10-01 10:00:00')"""
            )
            conn.commit()
        finally:
            conn.close()

        client = self.app.test_client()
        self._login_admin(client)

        # Searching photo OCR number should find the claim
        r1 = client.get("/admin/claims?q=TN38CX3972")
        self.assertIn("CLM-2025-000100", r1.get_data(as_text=True))

        # Searching old policy vehicle number should NOT find it (if policy number isn't searched)
        r2 = client.get("/admin/claims?q=TN38CT5038")
        self.assertNotIn("CLM-2025-000100", r2.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
