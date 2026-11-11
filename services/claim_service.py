"""Claim creation, listing and status updates."""
import json
import logging
import sqlite3



from datetime import datetime

from werkzeug.utils import secure_filename

from database.models import get_db
from services import file_service
from services.file_service import EVIDENCE_TYPES, UploadError

import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def validate_customer(form):
    """Customer details are optional; format is checked only when a value is given."""
    name = " ".join((form.get("customer_name") or "").split())
    raw_mobile = re.sub(r"[\s\-()]", "", form.get("mobile") or "")
    email = (form.get("email") or "").strip().lower()

    errors = {}
    mobile = ""

    if name and len(name) < 2:
        errors["customer_name"] = "Please enter your full name."

    if raw_mobile:
        digits = raw_mobile[3:] if raw_mobile.startswith("+91") else raw_mobile
        digits = digits[1:] if digits.startswith("0") and len(digits) == 11 else digits
        if re.fullmatch(r"[6-9]\d{9}", digits):
            mobile = "+91" + digits
        else:
            errors["mobile"] = "Enter a valid 10-digit mobile number."

    if email and not _EMAIL_RE.match(email):
        errors["email"] = "Enter a valid email address."

    return {
        "customer_name": name,
        "mobile": mobile,
        "email": email
    }, errors

log = logging.getLogger(__name__)

STATUSES = ["Pending Review", "Under Review", "Approved", "Rejected"]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _next_reference(db, year):
    prefix = f"CLM-{year}-"
    row = db.execute(
        "SELECT claim_reference FROM claims WHERE claim_reference LIKE ? "
        "ORDER BY claim_reference DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    seq = int(row["claim_reference"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{seq:06d}"


def missing_evidence(files):
    return [
        meta["short"]
        for key, meta in EVIDENCE_TYPES.items()
        if meta.get("required", True)
        and (not files.get(key) or not files[key].filename)
    ]

DIRECT_POLICY_NUMBER = "DIRECT-SUBMISSION"


def _direct_policy_id(db):
    """Hidden placeholder policy for claims submitted without a policy number."""
    row = db.execute(
        "SELECT id FROM policies WHERE policy_number = ?",
        (DIRECT_POLICY_NUMBER,)
    ).fetchone()

    if row:
        return row["id"]

    cur = db.execute(
        """INSERT INTO policies (
               policy_number, customer_name, customer_phone, vehicle_number,
               vehicle_make, vehicle_model, policy_status,
               policy_start_date, policy_end_date
           )
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            DIRECT_POLICY_NUMBER,
            "Direct submission",
            "-",
            "N/A",
            "-",
            "-",
            "Inactive",
            "2000-01-01",
            "2000-01-01"
        )
    )

    return cur.lastrowid

def create_claim(customer, files, photo_checks=None, vehicle_number=None, chassis=None,
                 ocr_vehicle=None, damage_photos=None):
    """Validate all files, store them, create claim + claim_files + claim_damage_photos atomically.

    `files` is a mapping of file_type -> werkzeug FileStorage.
    `photo_checks` maps photo slots to vehicle_image_validator results (all must be valid;
    routes/customer.py runs the checks). Their metadata is stored with each file.
    `vehicle_number` is the registration number read from photo OCR (or None).
    There is NEVER any fallback to the policy's vehicle number.
    `chassis` is the chassis_ocr_service result for the chassis photo.
    `ocr_vehicle` is the plate_ocr_service result for the vehicle plate photo.
    `damage_photos` is an optional list of werkzeug FileStorage items for additional damage evidence.
    Returns the claim reference. Raises UploadError for customer-facing problems.
    """
    photo_checks = photo_checks or {}
    chassis = chassis or {}
    ocr_vehicle = ocr_vehicle or {}
    extracted_vehicle_number = (ocr_vehicle.get("number") if ocr_vehicle else None) or vehicle_number or None
    damage_photos = [f for f in (damage_photos or []) if f and getattr(f, "filename", None)]

    missing = missing_evidence(files)

    if not damage_photos:
        missing.append("Damage photo (at least one)")

    if missing:
        raise UploadError(
            "Please complete your vehicle evidence. Missing: "
            + ", ".join(missing)
        )

    validated = {
        key: file_service.validate(key, files[key])
        for key in EVIDENCE_TYPES
        if files.get(key) and files[key].filename
    }

    validated_damage = [file_service.validate_damage_photo(dp) for dp in damage_photos]

    from services import damage_detection_service

    db = get_db()
    now = _now()
    reference = None
    for _attempt in range(5):
        try: 
            db.execute("BEGIN IMMEDIATE")
            reference = _next_reference(db, datetime.now().year)
            direct_id = _direct_policy_id(db)

            cur = db.execute(
                """INSERT INTO claims (
                       claim_reference, policy_id, policy_number, customer_name,
                       vehicle_number, submitted_at, status, created_at, updated_at,
                       chassis_number, chassis_ocr_status, chassis_ocr_confidence,
                       chassis_original_ocr_text, ocr_vehicle_number,
                       ocr_vehicle_status, ocr_vehicle_confidence, ocr_vehicle_raw_text,
                       mobile, email
                   )
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    reference,
                    direct_id,
                    "",
                    customer["customer_name"],
                    extracted_vehicle_number or "",
                    now,
                    STATUSES[0],
                    now,
                    now,
                    chassis.get("number"),
                    chassis.get("status"),
                    chassis.get("confidence"),
                    json.dumps(chassis.get("audit")) if chassis else None,
                    extracted_vehicle_number,
                    ocr_vehicle.get("status"),
                    ocr_vehicle.get("confidence"),
                    ocr_vehicle.get("raw_text"),
                    customer["mobile"] or None,
                    customer["email"] or None,
                ),
            )
            claim_id = cur.lastrowid
            for key, (ext, size) in validated.items():
                rel = file_service.save(reference, key, files[key], ext)
                check = photo_checks.get(key) or {}
                db.execute(
                    """INSERT INTO claim_files (claim_id, file_type, file_path, original_filename,
                                                mime_type, file_size, uploaded_at, expected_view,
                                                detected_view, confidence, validated_at,
                                                validation_status, validation_details)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (claim_id, key, rel, secure_filename(files[key].filename or "")[:200],
                     file_service.MIME.get(ext), size, now, check.get("expected_view"),
                     check.get("detected_view"), check.get("confidence"), check.get("validated_at"),
                     check.get("status"), json.dumps(check.get("details")) if check else None),
                )

            # Store and classify damage photos
            for idx, (dp, (ext, size)) in enumerate(zip(damage_photos, validated_damage), 1):
                dp_bytes = dp.read()
                dp.stream.seek(0)
                rel_dmg = file_service.save_damage_photo(reference, dp, ext, idx)
                classification = damage_detection_service.classify_damage_photo(dp_bytes)
                db.execute(
                    """INSERT INTO claim_damage_photos (claim_id, file_path, original_filename,
                                                        mime_type, file_size, uploaded_at,
                                                        damage_type, confidence, analysis_status,
                                                        analysis_details)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (claim_id, rel_dmg, secure_filename(dp.filename or "")[:200],
                     file_service.MIME.get(ext), size, now,
                     classification.get("damage_type") or "Other/uncertain",
                     classification.get("confidence"),
                     classification.get("status") or "UNAVAILABLE",
                     json.dumps(classification)),
                )

            db.commit()
            log.debug("Claim %s (id %s) stored with vehicle number %s and %d damage photos",
                      reference, claim_id, extracted_vehicle_number, len(damage_photos))
            return reference
        except sqlite3.IntegrityError as exc:
            db.rollback()
            if reference:
                file_service.remove_claim_folder(reference)
            if "claim_reference" in str(exc) or "UNIQUE" in str(exc):
                continue
            raise
        except Exception:
            db.rollback()
            if reference:
                file_service.remove_claim_folder(reference)
            raise
    raise RuntimeError("Could not allocate a claim reference.")


def get_stats():
    db = get_db()
    counts = {s: 0 for s in STATUSES}
    for row in db.execute("SELECT status, COUNT(*) n FROM claims GROUP BY status"):
        counts[row["status"]] = row["n"]
    today = datetime.now().strftime("%Y-%m-%d")
    counts["today"] = db.execute(
        "SELECT COUNT(*) FROM claims WHERE substr(submitted_at,1,10)=?", (today,)
    ).fetchone()[0]
    counts["total"] = sum(counts[s] for s in STATUSES)
    return counts


def list_claims(search="", statuses=None, limit=500):
    sql = """SELECT c.*, p.vehicle_make, p.vehicle_model
             FROM claims c JOIN policies p ON p.id = c.policy_id WHERE 1=1"""
    params = []
    if search:
        like = f"%{search.strip()}%"
        sql += """ AND (c.claim_reference LIKE ? OR c.policy_number LIKE ?
                   OR c.customer_name LIKE ? OR c.ocr_vehicle_number LIKE ?)"""
        params += [like] * 4
    if statuses:
        sql += f" AND c.status IN ({','.join('?' * len(statuses))})"
        params += list(statuses)
    sql += " ORDER BY c.submitted_at DESC, c.id DESC LIMIT ?"
    params.append(limit)

    from services.policy_service import normalize_registration, is_valid_registration
    rows = []
    for r in get_db().execute(sql, params):
        d = dict(r)
        if not d.get("customer_name"):
            d["customer_name"] = "Not provided"
        ocr_num = d.get("ocr_vehicle_number")
        if ocr_num:
            norm_ocr = normalize_registration(ocr_num)
            if 6 <= len(norm_ocr) <= 11 and is_valid_registration(norm_ocr):
                d["vehicle_number"] = norm_ocr
                d["ocr_vehicle_number"] = norm_ocr
            else:
                d["vehicle_number"] = None
                d["ocr_vehicle_number"] = None
        else:
            d["vehicle_number"] = None
            d["ocr_vehicle_number"] = None
        rows.append(d)
    return rows


def get_claim(claim_id):
    db = get_db()
    row = db.execute(
        """SELECT c.*, p.vehicle_make, p.vehicle_model, p.customer_phone,
                  p.policy_status, p.policy_start_date, p.policy_end_date,
                  p.vehicle_number AS policy_vehicle_number
           FROM claims c JOIN policies p ON p.id = c.policy_id WHERE c.id = ?""",
        (claim_id,),
    ).fetchone()
    if not row:
        return None, []
    row = dict(row)
    if row.get("mobile"):
        row["customer_phone"] = row["mobile"]
    elif not row.get("policy_number"):
        row["customer_phone"] = "Not provided"

    if not row.get("customer_name"):
        row["customer_name"] = "Not provided"
    from services.policy_service import normalize_registration, is_valid_registration
    from services.chassis_ocr_service import is_valid_vin, normalize_chassis
    row["policy_vehicle_number"] = normalize_registration(row.get("policy_vehicle_number"))
    ocr_num = row.get("ocr_vehicle_number")
    if ocr_num:
        norm_ocr = normalize_registration(ocr_num)
        if 6 <= len(norm_ocr) <= 11 and is_valid_registration(norm_ocr):
            row["ocr_vehicle_number"] = norm_ocr
            row["vehicle_number"] = norm_ocr
        else:
            row["ocr_vehicle_number"] = None
            row["vehicle_number"] = None
    else:
        row["ocr_vehicle_number"] = None
        row["vehicle_number"] = None

    chassis_num = row.get("chassis_number")
    if chassis_num:
        norm_chassis = normalize_chassis(chassis_num)
        if not is_valid_vin(norm_chassis) and row.get("chassis_ocr_status") != "MANUALLY_VERIFIED":
            row["chassis_number"] = None
        else:
            row["chassis_number"] = norm_chassis
    else:
        row["chassis_number"] = None
    try:
        row["chassis_audit"] = json.loads(row.get("chassis_original_ocr_text") or "null") or {}
    except ValueError:
        row["chassis_audit"] = {}
    files = {r["file_type"]: dict(r) for r in
             db.execute("SELECT * FROM claim_files WHERE claim_id = ?", (claim_id,))}
    all_types = dict(EVIDENCE_TYPES)
    all_types.update(file_service.LEGACY_EVIDENCE_TYPES)
    ordered = []
    for key, meta in all_types.items():
        if key in files:
            item = {**files[key], **meta, "file_type": key}
            try:
                item["validation"] = json.loads(item.get("validation_details") or "null") or {}
            except ValueError:
                item["validation"] = {}
            ordered.append(item)

    damage_photos = [dict(r) for r in db.execute(
        "SELECT * FROM claim_damage_photos WHERE claim_id = ? ORDER BY id", (claim_id,))]
    row["damage_photos"] = damage_photos
    return row, ordered


def get_damage_photo(photo_id):
    row = get_db().execute("SELECT * FROM claim_damage_photos WHERE id = ?", (photo_id,)).fetchone()
    return dict(row) if row else None


def update_chassis(claim_id, number, officer):
    """Claims-team confirmation/correction of the chassis number. Keeps the OCR audit text."""
    db = get_db()
    cur = db.execute(
        """UPDATE claims SET chassis_number=?, chassis_ocr_status='MANUALLY_VERIFIED', chassis_verified_by=?,
           updated_at=? WHERE id=?""", (number, officer, _now(), claim_id))
    db.commit()
    return cur.rowcount == 1


def claim_id_for(reference):
    row = get_db().execute("SELECT id FROM claims WHERE claim_reference = ?", (reference,)).fetchone()
    return row["id"] if row else None


def get_file(file_id):
    row = get_db().execute("SELECT * FROM claim_files WHERE id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def update_status(claim_id, status, reviewer, remarks=None):
    if status not in STATUSES:
        raise ValueError("Unknown status")

    # Remarks apply only to rejections.
    remarks = (remarks or "").strip()[:1000] if status == "Rejected" else ""

    db = get_db()
    cur = db.execute(
        "UPDATE claims SET status=?, reviewed_by=?, review_remarks=?, updated_at=? WHERE id=?",
        (status, reviewer, remarks or None, _now(), claim_id),
    )
    db.commit()
    return cur.rowcount == 1
