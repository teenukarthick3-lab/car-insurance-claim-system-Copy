"""Comprehensive test suite for the three targeted changes:
1. Customer flow - upload page only with policy number validation.
2. Damage analysis supporting all 12 visible damage types, photo deduplication, breakdown by type.
3. Repair estimation min-max ranges, uncertain exclusions, disclaimer, and re-run.
"""
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

import config
from app import create_app
from database import models
from database.seed import seed
from services import (
    damage_detection_service,
    estimation_service as est,
    vision_providers,
)
from tests.helpers import ScriptedProvider, make_photo, obs

VIDEO = b"\x00\x00\x00 ftypmp42\x00\x00\x00\x00isommp42\x00\x00\x00\x08free"


def make_damage(dtype="DENT", part="FRONT_BUMPER", sev="MINOR", conf=0.92, box=(0.1, 0.2, 0.3, 0.4), reason="Visible impact"):
    x0, y0, x1, y1 = box
    return {
        "damage_type": dtype.upper(),
        "vehicle_part": part.upper().replace(" ", "_"),
        "severity": sev.upper(),
        "confidence": conf,
        "box": {"x_min": x0, "y_min": y0, "x_max": x1, "y_max": y1},
        "reason": reason,
    }


class TestThreeTargetedChanges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        self.upload_dir = os.path.join(self.tmp, "uploads")
        os.makedirs(os.path.join(self.upload_dir, "claims"), exist_ok=True)

        self._saved_mode = config.PHOTO_VALIDATION_MODE
        config.PHOTO_VALIDATION_MODE = "scripted"
        damage_detection_service.clear_cache()

        self.ocr_patcher = patch("routes.customer._extract_with_gemini", return_value={"value": "TN38CT5038", "status": "format_valid", "raw_detected_text": "TN 38 CT 5038"})
        self.ocr_patcher.start()

        self.app = create_app({
            "TESTING": True,
            "DATABASE": self.db_path,
            "UPLOAD_FOLDER": self.upload_dir,
            "WTF_CSRF_ENABLED": False,
            "DAMAGE_ANALYSIS_SYNC": True,
        })
        models.init_db(self.db_path)
        seed(self.db_path, verbose=False)

        self.provider = ScriptedProvider()
        vision_providers.set_provider(self.provider)

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
        self.provider.register(self.photos["chassis_photo"], obs("chassis", vin_text="MA3EWDE1S00123456"))

        # Default damage registration: empty
        for photo in self.photos.values():
            self.provider.register_damage(photo, [])

    def tearDown(self):
        self.ocr_patcher.stop()
        config.PHOTO_VALIDATION_MODE = self._saved_mode
        vision_providers.set_provider(None)
        damage_detection_service.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get_csrf(self, client, url="/claim/evidence"):
        res = client.get(url, follow_redirects=True)
        html = res.get_data(as_text=True)
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return m.group(1) if m else "test-token"

    def _login_admin(self, client):
        csrf = self._get_csrf(client, "/admin/login")
        client.post("/admin/login", data={"csrf_token": csrf, "username": "admin", "password": "Admin@123"})

    # =========================================================================
    # CHANGE 1: Customer flow - upload page only
    # =========================================================================

    def test_change1_routes_redirect_straight_to_evidence(self):
        client = self.app.test_client()

        # / redirects to /claim/evidence
        r = client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/claim/evidence"))

        # /claim redirects to /claim/evidence
        r = client.get("/claim")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/claim/evidence"))

        # /claim/verify redirects to /claim/evidence
        r = client.get("/claim/verify")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/claim/evidence"))
        r = client.post("/claim/verify", data={})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/claim/evidence"))

        # /claim/confirm-vehicle redirects to /claim/evidence
        r = client.post("/claim/confirm-vehicle", data={})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/claim/evidence"))

        # /claim/confirm-registration redirects to /claim/evidence
        r = client.post("/claim/confirm-registration", data={})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/claim/evidence"))

    def test_change1_evidence_page_accessible_without_prior_session(self):
        client = self.app.test_client()
        r = client.get("/claim/evidence")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)

        # Has policy_number input
        self.assertIn('name="policy_number"', html)
        self.assertIn('id="policy_number"', html)

        # Must not show stored customer details or old confirm messages
        self.assertNotIn("Registration confirmed", html)
        self.assertNotIn("differs from your policy", html)
        self.assertNotIn("Maruti Swift", html)

    def test_change1_submit_with_missing_policy_shows_inline_error(self):
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        data = {
            "csrf_token": csrf,
            "policy_number": "",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        r = client.post("/claim/submit", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        html = r.get_data(as_text=True)
        self.assertIn("Enter your policy number", html)

        # Verify no claim was created in DB
        with models.connect(self.db_path) as db:
            cnt = db.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            self.assertEqual(cnt, 0)

    def test_change1_submit_with_invalid_policy_shows_inline_error(self):
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        data = {
            "csrf_token": csrf,
            "policy_number": "POL99999",  # Does not exist
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        r = client.post("/claim/submit", data=data, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 404)
        html = r.get_data(as_text=True)
        self.assertIn("Policy not found", html)

        # Verify no claim was created in DB
        with models.connect(self.db_path) as db:
            cnt = db.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            self.assertEqual(cnt, 0)

    def test_change1_submit_with_valid_policy_creates_claim(self):
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        data = {
            "csrf_token": csrf,
            "policy_number": "pol10001 ",  # Normalized: POL10001
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        r = client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Claim received", html)

        # Verify claim was created in DB with policy's vehicle number
        with models.connect(self.db_path) as db:
            row = db.execute("SELECT * FROM claims ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["policy_number"], "POL10001")
            self.assertEqual(row["vehicle_number"], "TN38CT5038")

    # =========================================================================
    # CHANGE 2: Damage analysis supports all visible damage types
    # =========================================================================

    def test_change2_all_12_damage_types_normalized_without_filtering(self):
        all_types = [
            "dent", "scratch", "crack", "paint_damage", "bumper_damage",
            "glass_damage", "lamp_damage", "panel_deformation",
            "broken_or_missing_part", "mirror_damage", "tyre_or_wheel_damage", "other"
        ]
        damages = [
            make_damage(dtype=t, part=f"part_{t}", sev="minor", conf=0.90)
            for t in all_types
        ]
        raw_report = {"image_assessable": True, "damages": damages, "notes": "all types visible"}
        normalized = vision_providers.normalize_damage_report(raw_report)

        norm_types = {d["damage_type"] for d in normalized["damages"]}
        for t in all_types:
            self.assertIn(t.upper(), norm_types, f"Damage type {t} should not be filtered out")

    def test_change2_unreturned_confidence_is_none_not_fabricated(self):
        damage_without_conf = {
            "damage_type": "dent",
            "vehicle_part": "bonnet",
            "severity": "minor",
            "box_2d": [0.1, 0.2, 0.3, 0.4],
            "reason": "Clear dent without confidence score",
        }
        normalized = vision_providers.normalize_damage_report({"damages": [damage_without_conf]})
        d = normalized["damages"][0]
        self.assertIsNone(d["confidence"], "Confidence must not be fabricated when model does not return it")

    def test_change2_multiple_damage_types_in_photo_all_returned(self):
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        # Front photo has bumper damage, crack, dent, scratch, and broken lamp
        multi_damage = [
            make_damage("bumper_damage", "front bumper", "moderate", 0.95),
            make_damage("crack", "front bumper", "minor", 0.90),
            make_damage("dent", "bonnet", "minor", 0.88),
            make_damage("scratch", "bonnet", "minor", 0.85),
            make_damage("lamp_damage", "left headlight", "severe", 0.92),
        ]
        self.provider.register_damage(self.photos["front_photo"], multi_damage)

        data = {
            "csrf_token": csrf,
            "policy_number": "POL10001",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        r = client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(r.status_code, 200)

        # Check in admin detail page
        self._login_admin(client)
        admin_page = client.get("/admin/claim/1").get_data(as_text=True)

        self.assertIn("Bumper damage", admin_page)
        self.assertIn("Crack", admin_page)
        self.assertIn("Dent", admin_page)
        self.assertIn("Scratch", admin_page)
        self.assertIn("Lamp damage", admin_page)
        self.assertIn("5 damage areas detected", admin_page)
        self.assertIn("Breakdown by type:", admin_page)

    def test_change2_photo_with_no_damage_returns_empty_and_no_estimate(self):
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        # All photos have no damage
        for p in self.photos.values():
            self.provider.register_damage(p, [])

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

    def test_change2_deduplication_and_damage_photos_included(self):
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        damage_photo1 = make_photo(seed=301)
        # Sighting 1 on front_photo: front bumper damage (moderate, conf 0.85)
        self.provider.register_damage(self.photos["front_photo"], [
            make_damage("bumper_damage", "front bumper", "moderate", 0.85)
        ])
        # Sighting 2 on damage_photo1: same front bumper damage (moderate, conf 0.95)
        self.provider.register_damage(damage_photo1, [
            make_damage("bumper_damage", "front bumper", "moderate", 0.95),
            make_damage("mirror_damage", "left wing mirror", "minor", 0.90)
        ])

        data = {
            "csrf_token": csrf,
            "policy_number": "POL10001",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
            "damage_photos": (io.BytesIO(damage_photo1), "dp1.jpg"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)

        with models.connect(self.db_path) as db:
            analysis = damage_detection_service.get_claim_analysis(db, 1)

            # Both front_photo and damage photo are in analyzed_photos
            analyzed_titles = [p["short"] for p in analysis["analyzed_photos"]]
            self.assertIn("Front", analyzed_titles)
            self.assertTrue(any("Damage" in t for t in analyzed_titles))

            # Merged confirmed damage count is 2 (bumper_damage counted once + mirror_damage)
            self.assertEqual(analysis["confirmed_count"], 2)

    # =========================================================================
    # CHANGE 3: Repair estimation driven by full damage analysis
    # =========================================================================

    def test_change3_rate_table_covers_all_12_types_with_min_max(self):
        all_types = [
            "DENT", "SCRATCH", "CRACK", "PAINT_DAMAGE", "BUMPER_DAMAGE",
            "GLASS_DAMAGE", "LAMP_DAMAGE", "PANEL_DEFORMATION",
            "BROKEN_OR_MISSING_PART", "MIRROR_DAMAGE", "TYRE_OR_WHEEL_DAMAGE", "OTHER"
        ]
        for t in all_types:
            self.assertIn(t, est.RATES, f"{t} must exist in RATES")
            for sev in ("MINOR", "MODERATE", "SEVERE"):
                self.assertIn(sev, est.RATES[t], f"{t} must have severity {sev}")
                rng = est.RATES[t][sev]
                self.assertIsInstance(rng, tuple)
                self.assertEqual(len(rng), 2)
                self.assertLessEqual(rng[0], rng[1], f"Min {rng[0]} must be <= max {rng[1]}")

    def test_change3_estimate_min_max_and_uncertain_excluded(self):
        detections = [
            make_damage("dent", "bonnet", "minor", 0.90),        # priced: (2500, 3500)
            make_damage("scratch", "left door", "minor", 0.92),   # priced: (1000, 2000)
            make_damage("crack", "windscreen", "uncertain", 0.95), # uncertain -> excluded
            make_damage("other", "antenna", "minor", 0.88),        # other -> excluded
        ]
        result = est.estimate(detections)

        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(len(result["unpriced"]), 2)
        self.assertEqual(result["excluded_count"], 2)

        # Min total: 2500 + 1000 = 3500
        # Max total: 3500 + 2000 = 5500
        self.assertEqual(result["total_min"], 3500)
        self.assertEqual(result["total_max"], 5500)

    def test_change3_admin_page_shows_estimate_and_exact_disclaimer(self):
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        self.provider.register_damage(self.photos["front_photo"], [
            make_damage("dent", "bonnet", "minor", 0.90),
            make_damage("glass_damage", "windscreen", "uncertain", 0.95),
        ])

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

        # Exact disclaimer required by user prompt
        disclaimer = "Sample estimate based on AI-detected damage. Final cost must be confirmed by an authorized claims assessor."
        self.assertIn(disclaimer, page)

        # Excluded item note
        self.assertIn("excluded from total estimate", page)
        self.assertIn("Needs manual assessment", page)

    def test_change3_rerun_analysis_updates_estimate(self):
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        # Initial damage: dent (minor: 2500 - 3500)
        self.provider.register_damage(self.photos["front_photo"], [
            make_damage("dent", "bonnet", "minor", 0.90)
        ])

        data = {
            "csrf_token": csrf,
            "policy_number": "POL10001",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)

        self._login_admin(client)
        page1 = client.get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("₹ 2,500 – ₹ 3,500", page1)

        # Update mock damage for re-run: severe bumper damage (9000 - 18000)
        self.provider.register_damage(self.photos["front_photo"], [
            make_damage("bumper_damage", "front bumper", "severe", 0.95)
        ])

        # Trigger re-run
        admin_csrf = self._get_csrf(client, "/admin/claim/1")
        r = client.post("/admin/claim/1/analyze", data={"csrf_token": admin_csrf}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)

        page2 = client.get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("₹ 9,000 – ₹ 18,000", page2)
        self.assertNotIn("₹ 2,500 – ₹ 3,500", page2)

    # =========================================================================
    # Admin dashboard, status updates, and regression testing
    # =========================================================================

    def test_admin_dashboard_and_status_updates(self):
        client = self.app.test_client()
        csrf = self._get_csrf(client)

        data = {
            "csrf_token": csrf,
            "policy_number": "POL10001",
            "damage_video": (io.BytesIO(VIDEO), "video.mp4"),
        }
        for slot, p_bytes in self.photos.items():
            data[slot] = (io.BytesIO(p_bytes), f"{slot}.jpg")

        client.post("/claim/submit", data=data, content_type="multipart/form-data", follow_redirects=True)

        self._login_admin(client)
        r = client.get("/admin/claims")
        self.assertEqual(r.status_code, 200)
        self.assertIn("POL10001", r.get_data(as_text=True))

        admin_csrf = self._get_csrf(client, "/admin/claim/1")
        r_update = client.post("/admin/claim/1/status", data={
            "csrf_token": admin_csrf,
            "status": "Approved",
            "decision_note": "Approved by assessor",
        }, follow_redirects=True)
        self.assertEqual(r_update.status_code, 200)
        self.assertIn("Approved", r_update.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
