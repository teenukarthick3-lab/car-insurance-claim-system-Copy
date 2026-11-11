"""Tests for AI damage analysis + repair estimation.  Run:  python -m unittest discover -s tests -t ."""
import hashlib
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

import config  # noqa: E402
config.CHASSIS_OCR_FALLBACK = "off"   # keep tests fast; OCR has its own tests
from app import create_app  # noqa: E402
from services import damage_detection_service as dds, estimation_service as est  # noqa: E402
from services import vehicle_image_validator as v, vision_providers  # noqa: E402
from tests.helpers import ScriptedProvider, damage, make_photo, obs  # noqa: E402

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000
KINDS = {"front_photo": "front", "back_photo": "rear", "left_photo": "left", "right_photo": "right",
         "chassis_photo": "chassis"}


def n(d):  # normalized detection as the service would produce it
    return vision_providers.normalize_damage_report({"damages": [d]})["damages"][0]


class Estimation(unittest.TestCase):
    def setUp(self):
        self._t = config.DAMAGE_CONFIDENCE_THRESHOLD
        config.DAMAGE_CONFIDENCE_THRESHOLD = 0.70

    def tearDown(self):
        config.DAMAGE_CONFIDENCE_THRESHOLD = self._t

    def test_rates_and_lines_add_up(self):
        e = est.estimate([n(damage("SCRATCH", "LEFT_FRONT_DOOR", "MINOR")),
                          n(damage("DENT", "LEFT_REAR_DOOR", "MINOR")),
                          n(damage("PAINT_DAMAGE", "BONNET", "MINOR"))])
        self.assertEqual([i["estimated_cost"] for i in e["items"]], [1500, 3000, 2500])
        self.assertEqual(e["total"], sum(e["lines"].values()))
        self.assertEqual(e["total"], 7000)
        self.assertEqual(set(e["lines"]), {"parts", "labour", "paint", "other"})

    def test_severity_scales_cost(self):
        self.assertEqual(est.item_cost("DENT", "MINOR"), 3000)
        self.assertEqual(est.item_cost("DENT", "SEVERE"), 7200)
        self.assertIsNone(est.item_cost("DENT", "UNCERTAIN"))

    def test_low_confidence_and_uncertain_are_not_priced(self):
        e = est.estimate([n(damage(confidence=0.5)), n(damage("DENT", "BONNET", "UNCERTAIN", 0.9))])
        self.assertEqual(e["total"], 0)
        self.assertEqual(len(e["possible"]), 1)
        self.assertEqual(len(e["unpriced"]), 1)

    def test_same_damage_in_two_photos_counted_once(self):
        a, b = n(damage(confidence=0.80)), n(damage(confidence=0.95))
        e = est.estimate([a, b])
        self.assertEqual(len(e["items"]), 1)
        self.assertEqual(e["items"][0]["confidence"], 0.95)

    def test_rates_are_configurable(self):
        old = est.RATE_CARD["SCRATCH"]["base"]
        try:
            est.RATE_CARD["SCRATCH"]["base"] = 2000
            self.assertEqual(est.item_cost("SCRATCH", "MINOR"), 2000)
        finally:
            est.RATE_CARD["SCRATCH"]["base"] = old

    def test_indian_money_format(self):
        self.assertEqual(est.format_money(20000, "₹"), "₹ 20,000")
        self.assertEqual(est.format_money(125000, "₹"), "₹ 1,25,000")
        self.assertEqual(est.format_money(950, "₹"), "₹ 950")


class Normalization(unittest.TestCase):
    def test_boxes_are_fixed_and_bad_ones_dropped(self):
        r = vision_providers.normalize_damage_report({"damages": [
            damage(box=(0.6, 0.8, 0.2, 0.3)),                    # reversed corners
            damage(box=(-0.2, 0.1, 1.4, 0.5)),                   # out of range
            damage(box=(0.5, 0.5, 0.501, 0.501)),                # too small
            {"damage_type": "laser", "severity": "huge", "confidence": 88, "box": {"x_min": 0.1, "y_min": 0.1,
                                                                                  "x_max": 0.3, "y_max": 0.3}},
        ]})
        boxes = [d["box"] for d in r["damages"]]
        self.assertEqual(len(boxes), 3)
        self.assertEqual(boxes[0], {"x": 0.2, "y": 0.3, "w": 0.4, "h": 0.5})
        self.assertEqual(boxes[1]["x"], 0.0)
        self.assertEqual(boxes[1]["w"], 1.0)
        self.assertEqual(r["damages"][2]["damage_type"], "OTHER")
        self.assertEqual(r["damages"][2]["severity"], "UNCERTAIN")
        self.assertAlmostEqual(r["damages"][2]["confidence"], 0.88)

    def test_no_damage_means_no_boxes(self):
        self.assertEqual(vision_providers.normalize_damage_report({"damages": []})["damages"], [])


class ClaimPage(unittest.TestCase):
    """Submit real claims through the app (scripted model), then open them as the claims team."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (config.PHOTO_VALIDATION_MODE, config.DAMAGE_ANALYSIS_MODE)
        config.PHOTO_VALIDATION_MODE, config.DAMAGE_ANALYSIS_MODE = "required", "auto"
        v.clear_cache()
        dds.clear_cache()
        self.provider = ScriptedProvider()
        vision_providers.set_provider(self.provider)
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "UPLOAD_FOLDER": self.tmp,
                               "DAMAGE_ANALYSIS_SYNC": True})
        self.photos = {}
        for i, (slot, kind) in enumerate(KINDS.items()):
            self.photos[slot] = make_photo(seed=500 + i)
            self.provider.register(self.photos[slot], obs(kind))

    def tearDown(self):
        config.PHOTO_VALIDATION_MODE, config.DAMAGE_ANALYSIS_MODE = self._saved
        vision_providers.set_provider(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def submit(self):
        c = self.app.test_client()
        token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/claim").get_data(as_text=True)).group(1)
        c.post("/claim/verify", data={"csrf_token": token, "policy_number": "POL10001"})
        c.post("/claim/confirm-vehicle", data={"csrf_token": token, "vehicle_number": "TN38CT5038"})
        data = {"csrf_token": token, "damage_video": (io.BytesIO(VIDEO), "v.mp4")}
        for slot, content in self.photos.items():
            data[slot] = (io.BytesIO(content), f"{slot}.jpg")
        r = c.post("/claim/submit", data=data, headers={"X-Requested-With": "fetch"}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["reference"]

    def admin(self):
        a = self.app.test_client()
        token = re.search(r'name="csrf_token" value="([^"]+)"', a.get("/admin/login").get_data(as_text=True)).group(1)
        a.post("/admin/login", data={"csrf_token": token, "username": "admin", "password": "Admin@123"})
        a.token = token
        return a

    def db(self):
        con = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        con.row_factory = sqlite3.Row
        return con

    def test_claim_with_damage_shows_analysis_and_estimate(self):
        self.provider.register_damage(self.photos["front_photo"], [
            damage("BUMPER_DAMAGE", "REAR_BUMPER", "MODERATE", 0.93, (0.25, 0.6, 0.75, 0.85)),
            damage("SCRATCH", "BOOT_TAILGATE", "MINOR", 0.88, (0.4, 0.3, 0.6, 0.4))])
        self.provider.register_damage(self.photos["back_photo"], [      # same bumper seen again
            damage("BUMPER_DAMAGE", "REAR_BUMPER", "MODERATE", 0.85, (0.3, 0.6, 0.7, 0.8))])
        self.provider.register_damage(self.photos["left_photo"], [
            damage("DENT", "LEFT_REAR_DOOR", "UNCERTAIN", 0.9, (0.5, 0.4, 0.6, 0.55)),
            damage("PAINT_DAMAGE", "LEFT_FRONT_DOOR", "MINOR", 0.55, (0.2, 0.4, 0.3, 0.5))])
        ref = self.submit()
        con = self.db()
        row = con.execute("SELECT * FROM claim_estimations").fetchone()
        self.assertEqual(row["analysis_status"], "COMPLETE")
        # bumper (5500*1.6=8800) once + scratch 1500 ; uncertain dent unpriced ; low-confidence paint excluded
        self.assertEqual(row["total_estimated_cost"], 10300)
        self.assertEqual(row["total_estimated_cost"],
                         row["parts_cost"] + row["labour_cost"] + row["paint_cost"] + row["other_cost"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM damage_assessments").fetchone()[0], 5)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM damage_assessments WHERE review_status='POSSIBLE'").fetchone()[0], 1)

        page = self.admin().get("/admin/claim/1").get_data(as_text=True)
        for text in ["AI damage analysis", "Original", "Damage analysis", "Damage detected", "Bumper damage",
                     "Possible — manual review", "Repair estimation", "Parts", "Labour", "Paint / Materials",
                     "Estimated total", "₹ 10,300", "Subject to final inspection", "Pending final inspection",
                     "AI-assisted assessment. Final damage and repair cost must be confirmed by an authorized claims assessor",
                     "Severity requires manual review", "Vehicle evidence", "Video", "Chassis number",
                     "Claim details", "Update status", ref]:
            self.assertIn(text, page, text)
        self.assertNotIn("Final repair cost", page)
        self.assertEqual(page.count('class="da-box'), 5)

    def test_original_photos_are_never_modified(self):
        self.provider.register_damage(self.photos["front_photo"], [damage()])
        self.submit()
        con = self.db()
        for f in con.execute("SELECT file_type, file_path FROM claim_files WHERE file_type LIKE '%photo'"):
            with open(os.path.join(self.tmp, f["file_path"]), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(),
                                 hashlib.sha256(self.photos[f["file_type"]]).hexdigest(), f["file_type"])
        files = sorted(os.listdir(os.path.join(self.tmp, "claims", os.listdir(os.path.join(self.tmp, "claims"))[0])))
        self.assertEqual(len(files), 6)              # no overlay images written next to the evidence

    def test_no_damage_found_is_not_invented(self):
        self.submit()
        page = self.admin().get("/admin/claim/1").get_data(as_text=True)
        self.assertEqual(page.count('class="da-box'), 0)
        self.assertIn("No visible damage detected in this photo", page)
        self.assertIn("No damage detected by AI", page)
        self.assertIn("₹ 0", page)

    def test_old_claim_without_analysis(self):
        self.submit()
        con = self.db()
        con.execute("DELETE FROM damage_assessments")
        con.execute("DELETE FROM claim_estimations")
        con.commit()
        r = self.admin().get("/admin/claim/1")
        self.assertEqual(r.status_code, 200)
        page = r.get_data(as_text=True)
        self.assertIn("AI analysis unavailable", page)
        self.assertIn("Review manually", page)
        self.assertIn("Estimate unavailable", page)
        self.assertIn("Run AI damage analysis", page)

    def test_no_ai_configured(self):
        vision_providers.set_provider(vision_providers.GeminiVisionProvider(api_key=""))
        config.PHOTO_VALIDATION_MODE = "auto"
        self.submit()
        self.assertEqual(self.db().execute("SELECT analysis_status FROM claim_estimations").fetchone()[0], "UNAVAILABLE")
        page = self.admin().get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("AI analysis unavailable", page)
        self.assertNotIn("Run AI damage analysis", page)

    def test_ai_failure_doesnt_break_claim_or_page(self):
        orig = self.provider.detect_damage
        self.provider.detect_damage = lambda *a, **k: (_ for _ in ()).throw(vision_providers.VisionProviderError("API down"))
        self.submit()
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
        page = self.admin().get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("AI analysis unavailable", page)
        self.assertIn("API down", page)
        self.provider.detect_damage = orig

    def test_pending_and_interrupted_states(self):
        self.submit()
        con = self.db()
        con.execute("UPDATE claim_estimations SET analysis_status='PENDING', updated_at=datetime('now','localtime')")
        con.commit()
        page = self.admin().get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("AI damage analysis in progress", page)
        self.assertIn("data-pending", page)
        con.execute("UPDATE claim_estimations SET updated_at='2020-01-01 00:00:00'")
        con.commit()
        page = self.admin().get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("interrupted", page)
        self.assertNotIn("data-pending", page)

    def test_rerun_requires_login_and_works(self):
        self.submit()
        anon = self.app.test_client()
        self.assertEqual(anon.post("/admin/claim/1/analyze").status_code, 302)       # to login
        a = self.admin()
        self.assertEqual(a.post("/admin/claim/1/analyze", data={"csrf_token": "bad"}).status_code, 400)
        self.provider.register_damage(self.photos["right_photo"], [damage("DENT", "RIGHT_FRONT_DOOR", "MINOR", 0.9)])
        dds.clear_cache()
        r = a.post("/admin/claim/1/analyze", data={"csrf_token": a.token}, follow_redirects=True)
        self.assertIn("₹ 3,000", r.get_data(as_text=True))


try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None


@unittest.skipIf(genai is None, "google-genai package not installed")
class GeminiDamageRequest(unittest.TestCase):
    def test_request_uses_damage_schema_and_view_hint(self):
        seen = {}

        def handler(*args, **kwargs):
            seen["kwargs"] = kwargs
            reply = {"image_assessable": True, "notes": "dent on door", "damages": [damage("DENT", "LEFT_FRONT_DOOR")]}
            class FakeResponse:
                text = json.dumps(reply)
            return FakeResponse()

        class FakeModels:
            def generate_content(self, *args, **kwargs):
                return handler(*args, **kwargs)

        class FakeClient:
            models = FakeModels()

        p = vision_providers.GeminiVisionProvider(api_key="k", model="gemini-3.5-flash-lite", client=FakeClient())
        report = vision_providers.normalize_damage_report(p.detect_damage(make_photo(seed=1), "vehicle's left side"))
        kwargs = seen["kwargs"]
        self.assertEqual(kwargs["config"].response_schema, vision_providers.DAMAGE_SCHEMA)
        self.assertIn("vehicle's left side", kwargs["contents"][1])
        self.assertEqual(report["damages"][0]["damage_type"], "DENT")


if __name__ == "__main__":
    unittest.main()
