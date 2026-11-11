"""Vehicle registration + chassis number tests.  Run:  python -m unittest discover -s tests -t .

The sample numbers below are TEST DATA only (they come from the feature request); the app itself
never contains them.
"""
import hashlib
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
config.CHASSIS_OCR_FALLBACK = "off"
from app import create_app  # noqa: E402
from services import chassis_ocr_service as ocr, policy_service  # noqa: E402
from services import vehicle_image_validator as v, vision_providers, damage_detection_service as dds  # noqa: E402
from tests.helpers import ScriptedProvider, make_photo, obs  # noqa: E402
from tests.plate_fixture import BOLD, REG, plate  # noqa: E402

TEST_REG_INPUT = "TN 38 CT 5038"
TEST_REG = "TN38CT5038"
TEST_VIN = "MA3JMT31SXG181197"
TEST_ENGINE = "K10BN5079864"
PLATE_LINES = ["MARUTI SUZUKI INDIA LTD.", "MADE IN INDIA", TEST_VIN, f"ENGINE NO. {TEST_ENGINE}"]
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000


class Registration(unittest.TestCase):
    def test_normalization(self):
        for raw in (TEST_REG_INPUT, "tn38ct5038", " TN-38-CT-5038 ", "TN.38.CT.5038"):
            self.assertEqual(policy_service.normalize_registration(raw), TEST_REG)

    def test_format(self):
        for ok in (TEST_REG, "KA05MN3456", "DL3CCT5038", "MH12A1", "22BH1234AA"):
            self.assertTrue(policy_service.is_valid_registration(ok), ok)
        for bad in ("", "HELLO", "1234567", TEST_VIN, "POL10001"):
            self.assertFalse(policy_service.is_valid_registration(policy_service.normalize_registration(bad)), bad)


class ChassisParsing(unittest.TestCase):
    def pick(self, lines, **kw):
        return ocr.decide([{"lines": lines, **kw}], "tesseract")

    def test_vin_chosen_not_engine(self):
        r = self.pick(PLATE_LINES)
        self.assertEqual(r["number"], TEST_VIN)
        self.assertNotEqual(r["number"], TEST_ENGINE)

    def test_engine_line_first(self):
        self.assertEqual(self.pick(["MARUTI SUZUKI INDIA LTD", f"ENGINE NO {TEST_ENGINE}", TEST_VIN])["number"], TEST_VIN)

    def test_engine_label_on_its_own_line(self):
        r = self.pick(["ENGINE NO.", TEST_ENGINE, "CHASSIS NO.", TEST_VIN])
        self.assertEqual(r["number"], TEST_VIN)

    def test_space_inserted_by_ocr(self):
        self.assertEqual(self.pick(["MA3JMT31S XG181197", f"ENGINE NO. {TEST_ENGINE}"])["number"], TEST_VIN)

    def test_punctuation_removed(self):
        self.assertEqual(ocr.normalize_chassis(" ma3jmt31s-xg181197. "), TEST_VIN)

    def test_only_engine_number_means_not_detected(self):
        self.assertEqual(self.pick(["MARUTI SUZUKI INDIA LTD", f"ENGINE NO. {TEST_ENGINE}"])["status"], ocr.NOT_DETECTED)

    def test_unreadable_character_is_not_dropped(self):
        self.assertEqual(self.pick(["MA3JMT31SXG18?197"])["status"], ocr.NOT_DETECTED)

    def test_ocr_letter_i_is_flagged_not_silently_trusted(self):
        r = ocr.decide([{"lines": ["MA3JMT3ISXG181197"]}, {"lines": [], "model_vin": "MA3JMT3ISXG181197"}], "ai")
        self.assertEqual(r["number"], TEST_VIN)
        self.assertEqual(r["status"], ocr.MANUAL_REVIEW)
        self.assertTrue(r["audit"]["corrections"])

    def test_ai_reads_that_agree_are_verified(self):
        r = ocr.from_photo_check({"confidence": 0.95, "details": {
            "plate_text_lines": PLATE_LINES, "vin_text": TEST_VIN, "engine_number_text": TEST_ENGINE,
            "vin_legibility": "CLEAR"}})
        self.assertEqual((r["number"], r["status"]), (TEST_VIN, ocr.VERIFIED))
        self.assertEqual(r["audit"]["engine_number"], TEST_ENGINE)
        self.assertEqual(r["audit"]["reads"][0]["lines"], PLATE_LINES)        # original text kept for audit

    def test_ai_confusing_engine_for_vin_is_caught(self):
        r = ocr.from_photo_check({"confidence": 0.95, "details": {
            "plate_text_lines": PLATE_LINES, "vin_text": TEST_ENGINE, "engine_number_text": TEST_ENGINE,
            "vin_legibility": "CLEAR"}})
        self.assertEqual(r["number"], TEST_VIN)

    def test_disagreement_needs_review(self):
        r = ocr.from_photo_check({"confidence": 0.95, "details": {
            "plate_text_lines": PLATE_LINES, "vin_text": "MA3JMT31SXG181198", "vin_legibility": "CLEAR"}})
        self.assertEqual(r["status"], ocr.MANUAL_REVIEW)

    def test_partial_legibility_needs_review(self):
        r = ocr.from_photo_check({"confidence": 0.95, "details": {
            "plate_text_lines": PLATE_LINES, "vin_text": TEST_VIN, "vin_legibility": "PARTIAL"}})
        self.assertEqual((r["number"], r["status"]), (TEST_VIN, ocr.MANUAL_REVIEW))

    def test_basic_ocr_alone_never_auto_verifies(self):
        self.assertEqual(ocr.decide([{"lines": PLATE_LINES}] * 3, "tesseract")["status"], ocr.MANUAL_REVIEW)

    def test_nothing_available(self):
        r = ocr.from_photo_check({"details": {}}, None)
        self.assertEqual((r["number"], r["status"]), (None, ocr.NOT_DETECTED))


@unittest.skipUnless(ocr.tesseract_cmd() and BOLD and REG, "Tesseract or test font not installed")
class RealOcrOnPlatePhotos(unittest.TestCase):
    """Real Tesseract OCR on rendered plate photos (no AI)."""

    def setUp(self):
        config.CHASSIS_OCR_FALLBACK = "tesseract"
        ocr._tess_cache.clear()

    def tearDown(self):
        config.CHASSIS_OCR_FALLBACK = "off"

    def read(self, lines, **kw):
        buf = io.BytesIO()
        plate([(t, BOLD if i != 1 else REG, size, gap) for i, (t, size, gap) in enumerate(lines)], **kw).save(buf, "JPEG", quality=90)
        return ocr.from_photo_check({}, buf.getvalue())

    def test_sample_layout(self):
        r = self.read([("MARUTI SUZUKI INDIA LTD.", 54, 26), ("MADE IN INDIA", 34, 40), (TEST_VIN, 76, 40),
                       (f"ENGINE NO. {TEST_ENGINE}", 50, 24)], seed=1)
        self.assertEqual(r["number"], TEST_VIN)
        self.assertEqual(r["status"], ocr.MANUAL_REVIEW)        # basic OCR → team confirms

    def test_engine_number_printed_first(self):
        r = self.read([("MARUTI SUZUKI INDIA LTD.", 54, 30), ("MADE IN INDIA", 34, 30),
                       (f"ENGINE NO. {TEST_ENGINE}", 50, 40), (TEST_VIN, 76, 40)], seed=3, angle=-2)
        self.assertEqual(r["number"], TEST_VIN)


class ClaimFlow(unittest.TestCase):
    """Customer enters the registration with spaces and uploads a chassis photo; claims team sees both."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._mode = config.PHOTO_VALIDATION_MODE
        config.PHOTO_VALIDATION_MODE = "required"
        v.clear_cache()
        dds.clear_cache()
        self.provider = ScriptedProvider()
        vision_providers.set_provider(self.provider)
        self.app = create_app({"DATABASE": os.path.join(self.tmp, "t.db"), "UPLOAD_FOLDER": self.tmp,
                               "DAMAGE_ANALYSIS_SYNC": True})
        kinds = {"front_photo": "front", "back_photo": "rear", "left_photo": "left", "right_photo": "right"}
        self.photos = {}
        for i, (slot, kind) in enumerate(kinds.items()):
            self.photos[slot] = make_photo(seed=700 + i)
            self.provider.register(self.photos[slot], obs(kind))
        self.photos["chassis_photo"] = make_photo(seed=799)
        self.provider.register(self.photos["chassis_photo"], obs(
            "chassis", plate_text_lines=PLATE_LINES, vin_text=TEST_VIN, engine_number_text=TEST_ENGINE))
        self.c = self.app.test_client()
        html = self.c.get("/claim").get_data(as_text=True)
        self.token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        self.c.post("/claim/verify", data={"csrf_token": self.token, "policy_number": "POL10001"})

    def confirm_default(self):
        self.confirm("TN38CT5038")

    def tearDown(self):
        config.PHOTO_VALIDATION_MODE = self._mode
        vision_providers.set_provider(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def confirm(self, number, **extra):
        return self.c.post("/claim/confirm-vehicle", data={"csrf_token": self.token, "vehicle_number": number, **extra})

    def submit(self, extra=None):
        data = {"csrf_token": self.token, "damage_video": (io.BytesIO(VIDEO), "v.mp4"), **(extra or {})}
        for slot, content in self.photos.items():
            data[slot] = (io.BytesIO(content), f"{slot}.jpg")
        return self.c.post("/claim/submit", data=data, headers={"X-Requested-With": "fetch"},
                           content_type="multipart/form-data")

    def row(self):
        con = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        con.row_factory = sqlite3.Row
        return con.execute("SELECT * FROM claims").fetchone()

    def admin_page(self):
        a = self.app.test_client()
        t = re.search(r'name="csrf_token" value="([^"]+)"', a.get("/admin/login").get_data(as_text=True)).group(1)
        a.post("/admin/login", data={"csrf_token": t, "username": "admin", "password": "Admin@123"})
        return a, t

    def test_full_flow_with_spaced_registration(self):
        diff_reg_input = "KA 01 AB 1234"
        diff_reg = "KA01AB1234"
        r = self.confirm(diff_reg_input)
        self.assertEqual(r.status_code, 302)                                     # saved straight away
        page = self.c.get("/claim/evidence").get_data(as_text=True)
        self.assertIn(diff_reg, page)
        self.assertIn("differs from your policy record", page)
        res = self.c.post("/claim/validate-photo", headers={"X-Requested-With": "fetch"}, content_type="multipart/form-data",
                          data={"csrf_token": self.token, "slot": "chassis_photo",
                                "photo": (io.BytesIO(self.photos["chassis_photo"]), "x.jpg")}).get_json()
        self.assertEqual(res["chassis"], {"number": TEST_VIN, "confident": True, "message": "Chassis number detected"})
        self.assertEqual(self.submit().status_code, 200)
        row = self.row()
        self.assertEqual(row["vehicle_number"], diff_reg)
        self.assertEqual(row["chassis_number"], TEST_VIN)
        self.assertEqual(row["chassis_ocr_status"], "VERIFIED")
        self.assertIn(TEST_ENGINE, row["chassis_original_ocr_text"])            # kept for audit only

        a, t = self.admin_page()
        page = a.get("/admin/claim/1").get_data(as_text=True)
        self.assertIn(diff_reg, page)
        self.assertIn(TEST_VIN, page)
        self.assertIn("Chassis number", page)
        self.assertIn("✓ Verified", page)
        self.assertIn("Confidence", page)
        self.assertIn("Policy record: TN38CT5038", page)                        # mismatch flagged
        details = page.split("Claim details", 1)[1]
        self.assertNotIn(f'<span class="chassis-no">{TEST_ENGINE}', details)

        r = a.post("/admin/claim/1/chassis", data={"csrf_token": t, "chassis_number": "ma3jmt31s xg181190"},
                   follow_redirects=True)
        self.assertIn("MA3JMT31SXG181190", r.get_data(as_text=True))
        self.assertEqual(self.row()["chassis_ocr_status"], "MANUALLY_VERIFIED")
        self.assertIn(TEST_ENGINE, self.row()["chassis_original_ocr_text"])     # audit kept after edit

    def test_no_silent_fallback_to_policy_number(self):
        # Regression: skipping the confirm step used to store the policy's registration instead.
        self.assertEqual(self.c.get("/claim/evidence").status_code, 302)
        r = self.submit()
        self.assertEqual(r.status_code, 409)
        self.assertIn("confirm your vehicle registration", r.get_json()["error"])
        self.assertIsNone(self.row())                                            # no claim created

    def test_entered_number_survives_without_second_confirmation(self):
        # Regression: the old "tap Continue again" mismatch step lost the typed number.
        self.confirm(TEST_REG_INPUT)
        self.c.get("/claim/evidence")
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.row()["vehicle_number"], TEST_REG)

    def test_each_claim_keeps_its_own_number(self):
        self.confirm(TEST_REG_INPUT)
        self.assertEqual(self.submit().status_code, 200)
        self.c.post("/claim/verify", data={"csrf_token": self.token, "policy_number": "POL10001"})
        self.confirm("ka 01 ab 1234")
        v.clear_cache()
        self.assertEqual(self.submit().status_code, 200)
        con = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        rows = dict(con.execute("SELECT id, vehicle_number FROM claims").fetchall())
        self.assertEqual(rows, {1: TEST_REG, 2: "KA01AB1234"})
        a, _ = self.admin_page()
        for cid, reg in rows.items():
            page = a.get(f"/admin/claim/{cid}").get_data(as_text=True)
            hero = page.split('class="facts"', 1)[1].split("</section>", 1)[0]
            self.assertIn(reg, hero)                                             # VEHICLE in the banner
            self.assertIn(f'<span class="plate">{reg}</span>', page)             # Vehicle number row

    def test_invalid_registration_rejected(self):
        r = self.confirm("HELLO WORLD")
        self.assertEqual(self.c.get("/claim/evidence").status_code, 302)          # still not confirmed
        self.assertEqual(r.status_code, 400)
        self.assertIn("doesn’t look like a vehicle registration number", r.get_data(as_text=True))

    def test_browser_cannot_inject_chassis_number(self):
        self.confirm(TEST_REG_INPUT)
        self.assertEqual(self.submit({"chassis_number": "FAKE1234567890123", "vehicle_number": "XX99ZZ9999"}).status_code, 200)
        row = self.row()
        self.assertEqual(row["chassis_number"], TEST_VIN)
        self.assertEqual(row["vehicle_number"], TEST_REG)

    def test_uncertain_chassis_goes_to_manual_review(self):
        self.confirm_default()
        self.provider.register(self.photos["chassis_photo"], obs(
            "chassis_partial", plate_text_lines=["MA3JMT31SXG18?197"], vin_text=None))
        v.clear_cache()
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.row()["chassis_ocr_status"], "NOT_DETECTED")
        a, _ = self.admin_page()
        page = a.get("/admin/claim/1").get_data(as_text=True)
        self.assertIn("Chassis number could not be confidently detected", page)
        self.assertIn("Manual review required", page)

    def test_original_chassis_image_kept(self):
        self.confirm_default()
        self.assertEqual(self.submit().status_code, 200)
        con = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        path = con.execute("SELECT file_path FROM claim_files WHERE file_type='chassis_photo'").fetchone()[0]
        with open(os.path.join(self.tmp, path), "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).digest(), hashlib.sha256(self.photos["chassis_photo"]).digest())

    def test_old_claim_without_chassis_fields(self):
        self.confirm_default()
        self.assertEqual(self.submit().status_code, 200)
        con = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        con.execute("UPDATE claims SET chassis_number=NULL, chassis_ocr_status=NULL, chassis_original_ocr_text=NULL")
        con.commit()
        a, _ = self.admin_page()
        r = a.get("/admin/claim/1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Manual review required", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
