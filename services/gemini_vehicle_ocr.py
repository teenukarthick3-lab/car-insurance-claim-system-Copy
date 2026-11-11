
import json
import logging
import os
import re
from functools import lru_cache

from google import genai
from google.genai import types

import config

logger = logging.getLogger(__name__)

SUPPORTED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}


@lru_cache(maxsize=4)
def get_gemini_client(api_key=None):
    """
    Create and cache a Gemini client.

    API key priority:
    1. Explicitly supplied API key
    2. config.GEMINI_API_KEY
    3. GEMINI_API_KEY environment variable
    """
    key = (
        api_key
        or getattr(config, "GEMINI_API_KEY", "")
        or os.getenv("GEMINI_API_KEY", "")
        or ""
    ).strip()

    if not key:
        raise RuntimeError(
            "Gemini API key is not configured. "
            "Set GEMINI_API_KEY in .env and restart Flask."
        )

    return genai.Client(api_key=key)


def _get_client(api_key=None):
    """Compatibility wrapper for existing integrations."""
    return get_gemini_client(api_key)


def _normalize_text(value):
    """Remove spaces and special characters and convert to uppercase."""
    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(value or "").upper(),
    )


def _valid_registration(value):
    """
    Validate a standard Indian vehicle registration number.

    Examples:
        TN38AB1234
        KA03MN5678
        KL07CD1234
    """
    value = _normalize_text(value)

    return bool(
        re.fullmatch(
            r"[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{1,4}",
            value,
        )
    )


def _valid_vin(value):
    """
    Validate a standard 17-character VIN/chassis number.

    Standard VINs exclude I, O and Q.
    Non-standard or shorter chassis identifiers will need review.
    """
    value = _normalize_text(value)

    return bool(
        re.fullmatch(
            r"[A-HJ-NPR-Z0-9]{17}",
            value,
        )
    )


def _mime_type(mime_type):
    """Validate the image MIME type before sending it to Gemini."""
    mime_type = (mime_type or "").lower().strip()

    if mime_type not in SUPPORTED_MIME_TYPES:
        raise ValueError("Unsupported image format.")

    return mime_type


def _parse_json_response(response_text):
    """Parse Gemini's JSON response, including optional code fences."""
    text = (response_text or "").strip()

    text = re.sub(
        r"\A```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*```\Z", "", text)

    parsed = json.loads(text)

    if not isinstance(parsed, dict):
        raise ValueError("Unexpected Gemini response format.")

    return parsed


def _result(
    value=None,
    status="not_found",
    raw_detected_text="",
):
    """
    Return a consistent result structure for existing integrations.
    """
    return {
        "value": value,
        "status": status,
        "raw_detected_text": str(
            raw_detected_text or ""
        ).strip(),
    }


def _build_prompt(image_kind):
    """Build a task-specific prompt for Gemini."""

    if image_kind == "registration_plate":
        return """
You are an OCR assistant for an Indian car insurance claim system.

Inspect the entire supplied image carefully and identify the vehicle's
Indian registration number, if it is visibly present.

Instructions:
- Read the actual characters visible in the image.
- Do not invent, guess, or complete missing characters.
- Ignore unrelated text, stickers, logos and advertisements.
- Preserve the distinction between similar characters such as
  0 and O, 1 and I, 5 and S, and 8 and B.
- Do not use a guessed registration number based on vehicle make,
  model, colour or other information.
- If the plate is blurred, cropped, obstructed or ambiguous,
  mark the result as uncertain.
- If no registration number is visible, return an empty string.
- Return JSON only.

Required JSON structure:
{
  "registration_number": "",
  "raw_detected_text": "",
  "uncertain": true
}

Field meanings:
- registration_number: the complete registration number only when
  the characters can be read confidently; otherwise an empty string.
- raw_detected_text: the characters actually observed, if any.
- uncertain: true when the reading is ambiguous or incomplete;
  otherwise false.
"""

    return """
You are an OCR assistant for an Indian car insurance claim system.

Inspect the supplied image and identify the vehicle's chassis number
or 17-character Vehicle Identification Number (VIN), if visible.

Instructions:
- Read the actual characters visible in the image.
- Do not invent, guess, or complete missing characters.
- Inspect the full visible chassis/VIN marking.
- Do not confuse the chassis/VIN with the engine number, registration
  number, model number, barcode or unrelated printed text.
- Standard VINs contain 17 characters and exclude I, O and Q.
- Preserve the visible character order.
- If the marking is blurred, cropped, obstructed or ambiguous,
  mark the result as uncertain.
- If no chassis/VIN number is visible, return an empty string.
- Return JSON only.

Required JSON structure:
{
  "chassis_number": "",
  "raw_detected_text": "",
  "uncertain": true
}

Field meanings:
- chassis_number: the complete chassis/VIN only when it can be read
  confidently; otherwise an empty string.
- raw_detected_text: the characters actually observed, if any.
- uncertain: true when the reading is ambiguous or incomplete;
  otherwise false.
"""


def extract_vehicle_details(
    image_bytes,
    mime_type,
    image_kind,
):
    """
    Extract a vehicle registration number or chassis/VIN from an image.

    Args:
        image_bytes: Image content as bytes.
        mime_type: Image MIME type, e.g. image/jpeg or image/png.
        image_kind: "registration_plate" or "chassis_plate".

    Returns:
        Dictionary:
        {
            "value": extracted value or None,
            "status": "format_valid" | "uncertain" |
                      "not_found" | "unavailable",
            "raw_detected_text": text observed by Gemini
        }

    Notes:
        - "format_valid" means the extracted value passes format
          validation; it does not establish vehicle ownership.
        - "uncertain" means the value requires review.
        - "not_found" means no usable value was found.
        - "unavailable" means OCR could not be completed.
    """

    if image_kind not in {
        "registration_plate",
        "chassis_plate",
    }:
        raise ValueError(
            "image_kind must be 'registration_plate' "
            "or 'chassis_plate'."
        )

    if not image_bytes:
        return _result(status="not_found")

    try:
        mime_type = _mime_type(mime_type)
    except ValueError:
        logger.warning(
            "Gemini vehicle OCR received an unsupported image format."
        )
        return _result(status="unavailable")

    if image_kind == "registration_plate":
        field_name = "registration_number"
        validator = _valid_registration
    else:
        field_name = "chassis_number"
        validator = _valid_vin

    try:
        client = _get_client()

        model_name = (
            getattr(config, "GEMINI_MODEL", None)
            or os.getenv("GEMINI_MODEL")
            or "gemini-3.5-flash-lite"
        )

        if image_kind == "registration_plate":
            ocr_schema = {
                "type": "object",
                "properties": {
                    "registration_number": {"type": "string"},
                    "raw_detected_text": {"type": "string"},
                    "uncertain": {"type": "boolean"},
                },
                "required": ["registration_number", "raw_detected_text", "uncertain"],
                
            }
        else:
            ocr_schema = {
                "type": "object",
                "properties": {
                    "chassis_number": {"type": "string"},
                    "raw_detected_text": {"type": "string"},
                    "uncertain": {"type": "boolean"},
                },
                "required": ["chassis_number", "raw_detected_text", "uncertain"],
                
            }

        response = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=mime_type,
                ),
                _build_prompt(image_kind),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ocr_schema,
                temperature=0,
            ),
        )

        response_text = getattr(response, "text", None)

        if not response_text:
            logger.warning(
                "Gemini returned an empty vehicle OCR response."
            )
            return _result(status="unavailable")

        parsed = _parse_json_response(response_text)

        candidate = parsed.get(field_name)
        raw_text = str(
            parsed.get("raw_detected_text") or ""
        ).strip()

        uncertain = parsed.get("uncertain", True)

        # Only an explicit boolean False indicates a confident reading.
        if not isinstance(uncertain, bool):
            uncertain = True

        normalized = _normalize_text(candidate)

        if not normalized:
            status = "uncertain" if uncertain else "not_found"

            return _result(
                value=None,
                status=status,
                raw_detected_text=raw_text,
            )

        # Never accept a value that fails the expected format.
        if not validator(normalized):
            logger.warning(
                "Gemini OCR %s failed validation: candidate=%r raw=%r",
                image_kind,
                candidate,
                raw_text,
            )
            return _result(
                value=None,
                status="uncertain",
                raw_detected_text=raw_text or str(candidate),
            )

        # A valid-looking number must still be confidently read.
        if uncertain:
            return _result(
                value=None,
                status="uncertain",
                raw_detected_text=raw_text or str(candidate),
            )

        return _result(
            value=normalized,
            status="format_valid",
            raw_detected_text=raw_text or str(candidate),
        )

    except Exception:
        # Keep technical details in server logs, not customer responses.
        logger.exception(
            "Gemini vehicle OCR failed for image_kind=%s",
            image_kind,
        )

        return _result(status="unavailable")
