"""Customer (mobile) claim flow: policy lookup -> confirm -> evidence -> submit -> success."""


import logging

import mimetypes


from flask import (

    Blueprint,

    abort,

    current_app,

    jsonify,

    redirect,

    render_template,

    request,

    session,

    url_for,

)


from security import check_csrf

import config


from services import (

    chassis_ocr_service,

    claim_service,

    damage_detection_service,

    file_service,

    gemini_vehicle_ocr,

    plate_ocr_service,

    policy_service,

    vehicle_image_validator as photo_ai,

)

from services.file_service import (

    EVIDENCE_TYPES,

    MAX_IMAGE_BYTES,

    MAX_VIDEO_BYTES,

    UploadError,

)


bp = Blueprint("customer", __name__)

log = logging.getLogger(__name__)


def _verified_policy():

    """Return the policy verified in this session, rechecked against the DB."""

    pid = session.get("verified_policy_id")


    if not pid:

        return None


    policy = policy_service.get_policy_by_id(pid)


    if not policy or not policy_service.eligibility(policy)[0]:

        session.pop("verified_policy_id", None)

        return None


    return policy


def _image_mime_type(storage):

    """Resolve the uploaded image MIME type using the existing upload validation."""

    mime_type = (getattr(storage, "mimetype", None) or "").lower()


    if mime_type in gemini_vehicle_ocr.SUPPORTED_MIME_TYPES:

        return mime_type


    mime_type = mimetypes.guess_type(

        getattr(storage, "filename", "") or ""

    )[0]


    if mime_type in gemini_vehicle_ocr.SUPPORTED_MIME_TYPES:

        return mime_type


    raise ValueError("Unsupported image format.")


def _extract_with_gemini(storage, image_kind):

    """Read an uploaded image and extract its registration or chassis number."""

    image_bytes = storage.read()

    storage.stream.seek(0)


    result = gemini_vehicle_ocr.extract_vehicle_details(

        image_bytes=image_bytes,

        mime_type=_image_mime_type(storage),

        image_kind=image_kind,

    )


    return result


def _chassis_status_for_storage(result):

    """

    Map Gemini extraction to the existing chassis service's status convention.


    A format-valid VIN is not proof of authenticity, so it is not marked

    VERIFIED here. The claims team should confirm the extracted number.

    """

    if result.get("status") == "format_valid":

        return getattr(

            chassis_ocr_service,

            "MANUAL_REVIEW",

            "MANUAL_REVIEW",

        )


    return getattr(

        chassis_ocr_service,

        "MANUAL_REVIEW",

        "MANUAL_REVIEW",

    )


@bp.route("/")
def root():
    return redirect(url_for("customer.evidence"))


@bp.route("/claim", methods=["GET"])
def welcome():
    return redirect(url_for("customer.evidence"))


@bp.route("/claim/verify", methods=["GET", "POST"])
def verify():
    return redirect(url_for("customer.evidence"))


def _render_verify(
    policy,
    eligible,
    code=None,
    message=None,
    vehicle_value=None,
    vehicle_error=None,
    vehicle_mismatch=False,
    status=200,
    vehicle_prompt=False,
):
    return render_template(
        "customer/verify.html",
        policy=policy,
        eligible=eligible,
        code=code,
        message=message,
        masked_phone=policy_service.mask_phone(
            policy["customer_phone"]
        ),
        fmt=policy_service.format_date,
        vehicle_value=(
            vehicle_value
            or session.get("claim_vehicle_number")
            or policy["vehicle_number"]
        ),
        vehicle_error=vehicle_error,
        vehicle_mismatch=vehicle_mismatch,
        vehicle_prompt=vehicle_prompt,
    ), status


@bp.route("/claim/confirm-vehicle", methods=["GET", "POST"])
def confirm_vehicle():
    return redirect(url_for("customer.evidence"))


@bp.route("/claim/confirm-registration", methods=["GET", "POST"])
def confirm_registration():
    return redirect(url_for("customer.evidence"))


@bp.route("/claim/evidence", methods=["GET"])
def evidence():
    return render_template(
        "customer/evidence.html",
        evidence_types=EVIDENCE_TYPES,
        max_image_mb=MAX_IMAGE_BYTES // (1024 * 1024),
        max_video_mb=MAX_VIDEO_BYTES // (1024 * 1024),
        values={},
        field_errors={},
    )


def _public_chassis(chassis):

    """Return only customer-safe chassis information."""

    confident = (

        chassis.get("status") == chassis_ocr_service.VERIFIED

    )


    return {

        "number": chassis.get("number") or None,

        "confident": confident,

        "message": (

            "Chassis number detected"

            if confident and chassis.get("number")

            else (

                "Chassis number detected — our team will confirm it"

                if chassis.get("number")

                else (

                    "Chassis number could not be confidently detected. "

                    "Our team will verify it from your photo."

                )

            )

        ),

    }


def _public_check(slot, result):

    """Return the photo validation fields intended for the browser."""

    return {

        "slot": slot,

        "valid": result["valid"],

        "status": result["status"],

        "code": result["code"],

        "title": result["title"],

        "message": result["message"],

        "retryable": result.get("retryable", False),

    }


@bp.route("/claim/validate-photo", methods=["POST"])
def validate_photo():
    """Validate a selected photo and extract numbers using Gemini."""
    check_csrf()

    slot = request.form.get("slot", "")


    if slot not in photo_ai.SLOT_EXPECTED_VIEW:

        return jsonify(

            ok=False,

            error="Unknown photo type.",

        ), 400


    used = session.get("photo_checks", 0)


    if used >= config.MAX_PHOTO_CHECKS_PER_SESSION:

        return jsonify(

            ok=False,

            error=(

                "Too many photo attempts. Please start your claim "

                "again or call 1800-123-4567."

            ),

        ), 429


    session["photo_checks"] = used + 1

    storage = request.files.get("photo")


    try:

        file_service.validate(slot, storage)

    except UploadError as exc:

        return jsonify(

            ok=True,

            **_public_check(

                slot,

                {

                    "valid": False,

                    "status": "REJECTED",

                    "code": "BAD_FILE",

                    "title": "Photo can't be used",

                    "message": str(exc),

                },

            ),

        )


    try:

        data = storage.read()

        storage.stream.seek(0)


        # Keep existing vehicle-view/photo validation unchanged.

        result = photo_ai.validate_slot(slot, data)

        extra = {}


        if slot == "chassis_photo" and result["valid"]:

            chassis_info = chassis_ocr_service.from_photo_check(result, data)

            if not chassis_info.get("number"):

                try:

                    c_res = _extract_with_gemini(storage, "chassis_plate")

                    if c_res.get("value"):

                        chassis_info = {

                            "number": c_res["value"],

                            "status": _chassis_status_for_storage(c_res),

                        }

                except Exception:

                    pass

            extra["chassis"] = _public_chassis(chassis_info)


        elif slot in {"front_photo", "back_photo"} and result["valid"]:

            # Prefer Gemini on the actual uploaded vehicle image. This is not

            # official registration verification; uncertain results stay manual.

            reg_info = {}

            try:

                r_res = _extract_with_gemini(storage, "registration_plate")

                if r_res.get("value"):

                    reg_info = {

                        "number": r_res["value"],

                        "status": r_res.get("status", "uncertain"),

                        "raw_text": r_res.get("raw_detected_text", ""),

                    }

            except Exception:

                log.exception("Gemini registration extraction failed during photo validation")


            # Keep the existing OCR as a fallback when Gemini cannot read it.

            if not reg_info.get("number") and slot == "front_photo":

                try:

                    legacy_info = plate_ocr_service.extract_registration(result, data)

                    if legacy_info.get("number"):

                        reg_info = legacy_info

                except Exception:

                    log.exception("Fallback registration OCR failed during photo validation")


            if reg_info.get("number"):

                is_format_valid = reg_info.get("status") in (

                    plate_ocr_service.VERIFIED,

                    "format_valid",

                    "FORMAT_VALID",

                )

                extra["registration"] = {

                    "number": reg_info["number"],

                    "status": "format_valid" if is_format_valid else "uncertain",

                    "message": "Number detected; our team will confirm it.",

                }


    except Exception:

        log.exception("Photo validation crashed")

        return jsonify(

            ok=False,

            error=(

                "We couldn't check this photo right now. "

                "Please try again."

            ),

        ), 500


    return jsonify(

        ok=True,

        **_public_check(slot, result),

        **extra,

    )


def _recheck_photos(files):

    """

    Revalidate submitted vehicle photos on the server.


    The browser's earlier checks are never trusted. Existing validator

    behavior is preserved.

    """

    images = {}


    for slot in photo_ai.PHOTO_SLOTS:

        storage = files[slot]

        images[slot] = storage.read()

        storage.stream.seek(0)


    checks = photo_ai.validate_slots(images)


    failures = [

        (slot, checks[slot])

        for slot in photo_ai.PHOTO_SLOTS

        if not checks[slot]["valid"]

    ]


    return checks, failures


@bp.route("/claim/submit", methods=["POST"])
def submit():
    wants_json = (
        request.headers.get("X-Requested-With") == "fetch"
    )

    def fail(msg, code=400, rejected=None, errors=None):
        if wants_json:
            return jsonify(
                ok=False,
                error=msg,
                errors=errors or {},
                rejected=rejected or [],
            ), code

        return render_template(
            "customer/evidence.html",
            evidence_types=EVIDENCE_TYPES,
            max_image_mb=MAX_IMAGE_BYTES // (1024 * 1024),
            max_video_mb=MAX_VIDEO_BYTES // (1024 * 1024),
            values=request.form,
            field_errors=errors or {},
            server_error=msg,
        ), code

    check_csrf()

    customer, field_errors = claim_service.validate_customer(request.form)
    if field_errors:
        return fail("Please correct your details.", 400, errors=field_errors)



    vehicle_number = None


    files = {

        key: request.files.get(key)

        for key in EVIDENCE_TYPES

    }


    damage_photos = [

        item

        for item in request.files.getlist("damage_photos")

        if item and getattr(item, "filename", None)

    ]


    try:

        missing = claim_service.missing_evidence(files)

        if not damage_photos:
            missing.append("Damage photo (at least one)")

        if missing:
            raise UploadError(
                "Please complete your vehicle evidence. Missing: "
                + ", ".join(missing)
            )

        for key in EVIDENCE_TYPES:
            if files.get(key) and files[key].filename:
                file_service.validate(key, files[key])


        for damage_photo in damage_photos:

            file_service.validate_damage_photo(damage_photo)


        checks, failures = _recheck_photos(files)


        if failures:

            rejected = [

                _public_check(slot, result)

                for slot, result in failures

            ]


            first_slot, first = failures[0]


            all_types = dict(EVIDENCE_TYPES)

            all_types.update(

                file_service.LEGACY_EVIDENCE_TYPES

            )


            label = all_types.get(

                first_slot, {}

            ).get("label", first_slot)


            unavailable = all(

                result["code"] == photo_ai.UNAVAILABLE

                for _, result in failures

            )


            return fail(

                f"{label}: {first['title']}. {first['message']}",

                503 if unavailable else 400,

                rejected,

            )


        # Extract chassis number using chassis OCR service (AI photo check + optional Gemini / Tesseract)

        chassis_storage = files.get("chassis_photo")

        chassis_data = chassis_storage.read() if chassis_storage else b""

        if chassis_storage:

            chassis_storage.stream.seek(0)

        chassis = chassis_ocr_service.from_photo_check(checks.get("chassis_photo"), chassis_data)

        if not chassis.get("number") and chassis_storage:

            try:

                chassis_result = _extract_with_gemini(chassis_storage, "chassis_plate")

                if chassis_result.get("value"):

                    chassis = {

                        "number": chassis_result["value"],

                        "status": _chassis_status_for_storage(chassis_result),

                        "confidence": 0.90 if chassis_result.get("status") == "format_valid" else 0.50,

                        "audit": {"source": "gemini", "raw": chassis_result.get("raw_detected_text", "")},

                        "message": "Vehicle chassis number detected.",

                    }

            except Exception:

                log.exception("Chassis Gemini extraction failed")


        # Extract registration from uploaded car photos using Gemini first.

        # Try the front view, then the back view if the plate is not readable.

        # The customer-confirmed vehicle_number remains unchanged; OCR is stored

        # separately for assessment and manual verification.

        ocr_vehicle = {

            "number": None,

            "status": getattr(plate_ocr_service, "MANUAL_REVIEW", "MANUAL_REVIEW"),

            "confidence": 0.0,

            "raw_text": "",

            "message": "Vehicle registration could not be read automatically; manual review required.",

        }


        registration_result = None

        for photo_slot in ("front_photo", "back_photo"):

            photo_storage = files.get(photo_slot)

            if not photo_storage:

                continue


            try:

                candidate_result = _extract_with_gemini(

                    photo_storage,

                    "registration_plate",

                )
                log.info(
                    "Gemini registration OCR result: slot=%s status=%s value_detected=%s",
                    photo_slot,
                    candidate_result.get("status"),
                    bool(candidate_result.get("value")),
                )

                if candidate_result.get("value") and candidate_result.get("status") == "format_valid":

                    registration_result = candidate_result

                    break

                if candidate_result.get("raw_detected_text") and registration_result is None:

                    registration_result = candidate_result

            except Exception:

                log.exception(

                    "Gemini registration extraction failed for %s",

                    photo_slot,

                )


        # Preserve the existing OCR as a fallback if Gemini could not provide

        # a usable number from either view.

        if not registration_result or not registration_result.get("value"):

            front_storage = files.get("front_photo")

            if front_storage:

                front_data = front_storage.read()

                front_storage.stream.seek(0)

                try:

                    legacy_result = plate_ocr_service.extract_registration(

                        checks.get("front_photo"),

                        front_data,

                    )

                    if legacy_result.get("number"):

                        ocr_vehicle = legacy_result

                except Exception:

                    log.exception("Fallback vehicle registration OCR failed")


        if registration_result and registration_result.get("value"):

            is_format_valid = registration_result.get("status") == "format_valid"

            ocr_vehicle = {

                "number": registration_result["value"],

                "status": (

                    plate_ocr_service.VERIFIED

                    if is_format_valid

                    else getattr(plate_ocr_service, "MANUAL_REVIEW", "MANUAL_REVIEW")

                ),

                "confidence": 0.90 if is_format_valid else 0.50,

                "raw_text": registration_result.get("raw_detected_text", ""),

                "message": (

                    "Vehicle registration number detected; manual confirmation required."

                ),

                "audit": {"source": "gemini"},

            }


        vehicle_number = (ocr_vehicle.get("number") if ocr_vehicle else None) or None
        reference = claim_service.create_claim(
            customer,
            files,
            photo_checks=checks,
            vehicle_number=vehicle_number,

            chassis=chassis,

            ocr_vehicle=ocr_vehicle,

            damage_photos=damage_photos,

        )


    except UploadError as exc:

        return fail(str(exc))


    except Exception:

        log.exception("Claim submission failed")

        return fail(

            "We couldn't submit your claim right now. "

            "Please try again.",

            500,

        )


    # Damage analysis remains unchanged and must not block submission.

    try:

        damage_detection_service.start_background_analysis(

            current_app.config["DATABASE"],

            current_app.config["UPLOAD_FOLDER"],

            claim_service.claim_id_for(reference),

            wait=current_app.config.get(

                "DAMAGE_ANALYSIS_SYNC",

                False,

            ),

        )

    except Exception:

        log.exception(

            "Could not start damage analysis for %s",

            reference,

        )


    session.pop("verified_policy_id", None)

    session.pop("photo_checks", None)

    session.pop("claim_vehicle_number", None)

    session["last_claim"] = reference


    target = url_for(

        "customer.success",

        reference=reference,

    )


    if wants_json:

        return jsonify(

            ok=True,

            redirect=target,

            reference=reference,

        )


    return redirect(target)


@bp.route("/claim/success/<reference>")

def success(reference):

    """Only the device that submitted the claim can see its confirmation."""

    if session.get("last_claim") != reference:

        abort(404)


    return render_template(

        "customer/success.html",

        reference=reference,

    )
