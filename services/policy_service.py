"""Policy lookup. Replace the body of find_policy() to point at a real policy system."""
import re
from datetime import date

from database.models import get_db

POLICY_RE = re.compile(r"^[A-Z]{3}\d{4,10}$")
# Indian registration numbers: state code + RTO number + series + number (e.g. XX 00 XX 0000),
# legacy 4-digit format without series (e.g. XX 00 0000), plus the Bharat series (00 BH 0000 X).
REGISTRATION_RE = re.compile(r"^([A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4}|[A-Z]{2}\d{1,2}\d{4}|\d{2}BH\d{4}[A-Z]{1,2})$")


def normalize_registration(raw):
    """Vehicle registration number for storage/display: uppercase, no spaces/dashes/dots."""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def is_valid_registration(number):
    if not number:
        return False
    norm = normalize_registration(number)
    if len(norm) < 6 or len(norm) > 11:
        return False
    return bool(REGISTRATION_RE.match(norm))


def normalize_policy_number(raw):
    return re.sub(r"[\s-]", "", (raw or "")).upper()


def is_valid_format(policy_number):
    return bool(POLICY_RE.match(policy_number))


def _policy(row):
    if not row:
        return None
    p = dict(row)
    p["vehicle_number"] = normalize_registration(p.get("vehicle_number"))
    return p


def find_policy(policy_number):
    return _policy(get_db().execute("SELECT * FROM policies WHERE policy_number = ?", (policy_number,)).fetchone())


def get_policy_by_id(policy_id):
    return _policy(get_db().execute("SELECT * FROM policies WHERE id = ?", (policy_id,)).fetchone())


def eligibility(policy):
    """Return (eligible: bool, code, message) for starting a claim."""
    today = date.today().isoformat()
    status = (policy.get("policy_status") or "").strip().lower()
    if status != "active":
        return False, "inactive", (
            "This policy is not active, so a claim can't be started online. "
            "Please contact our support team to reactivate your cover."
        )
    if policy["policy_end_date"] < today:
        return False, "expired", (
            f"This policy expired on {format_date(policy['policy_end_date'])}. "
            "Please renew your policy or contact support for help with this claim."
        )
    if policy["policy_start_date"] > today:
        return False, "not_started", (
            f"This policy starts on {format_date(policy['policy_start_date'])}. "
            "Claims can be submitted once cover has begun."
        )
    return True, "active", "Active"


def mask_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    return f"•••• ••{digits[-4:]}" if len(digits) >= 4 else ""


def format_date(iso):
    try:
        return date.fromisoformat(iso).strftime("%d %b %Y")
    except (TypeError, ValueError):
        return iso or ""
