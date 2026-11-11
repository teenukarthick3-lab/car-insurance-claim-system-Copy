"""Small security helpers: CSRF tokens and the admin login guard."""
import hmac
import secrets
from functools import wraps

from flask import abort, redirect, request, session, url_for


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def check_csrf():
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf", "")
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, description="Your session expired. Please refresh the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("admin.login", next=request.full_path.rstrip("?") if request.method == "GET" else None))
        return view(*args, **kwargs)
    return wrapper
