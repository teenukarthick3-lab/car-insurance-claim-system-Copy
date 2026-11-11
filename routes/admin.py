"""Insurance team (desktop) portal."""
from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from database.models import get_db
from security import check_csrf, login_required
from services import claim_service, file_service, policy_service
from services import chassis_ocr_service, damage_detection_service, estimation_service
from services.vehicle_image_validator import validation_status

bp = Blueprint("admin", __name__, url_prefix="/admin")

VIEWS = {
    "dashboard": {"title": "Recent Claims", "statuses": None},
    "all":     {"title": "All Claims",     "statuses": None},
    "pending":  {"title": "Pending Claims", "statuses": ["Pending Review", "Under Review"]},
    "reviewed": {"title": "Reviewed Claims", "statuses": ["Approved", "Rejected"]},
    "approved": {"title": "Approved Claims", "statuses": ["Approved"]},
    "rejected": {"title": "Rejected Claims", "statuses": ["Rejected"]},
}


@bp.app_template_filter("dt")
def format_dt(value, fmt="%d %b %Y, %I:%M %p"):
    from datetime import datetime
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime(fmt)
    except (TypeError, ValueError):
        return value or ""


@bp.app_template_filter("status_class")
def status_class(status):
    return {"Pending Review": "pending", "Under Review": "review",
            "Approved": "approved", "Rejected": "rejected"}.get(status, "pending")


@bp.app_template_filter("view_name")
def view_name(view):
    return {"FRONT": "Front", "REAR": "Rear", "LEFT_SIDE": "Left side", "RIGHT_SIDE": "Right side",
            "CHASSIS": "Chassis/VIN", "UNKNOWN": "Unknown"}.get(view or "", "—")


@bp.app_template_filter("filesize")
def filesize(n):
    n = n or 0
    return f"{n / (1024 * 1024):.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


@bp.app_context_processor
def nav_counts():
    if request.path.startswith("/admin") and session.get("admin_id"):
        try:
            s = claim_service.get_stats()
            return {"nav_pending": s["Pending Review"] + s["Under Review"]}
        except Exception:
            pass
    return {}


@bp.route("/")
def index():
    return redirect(url_for("admin.dashboard"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_id"):
        return redirect(url_for("admin.dashboard"))
    error = None
    if request.method == "POST":
        check_csrf()
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = get_db().execute("SELECT * FROM admin_users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            csrf = session.get("_csrf")
            session.clear()                      # prevent session fixation
            session["_csrf"] = csrf
            session["admin_id"] = user["id"]
            session["admin_name"] = user["full_name"]
            session["admin_username"] = user["username"]
            session.permanent = True
            nxt = request.args.get("next") or ""
            if not nxt.startswith("/admin/") or nxt.startswith("//"):
                nxt = url_for("admin.dashboard")
            return redirect(nxt)
        error = "Invalid username or password."
    return render_template("admin/login.html", error=error), (401 if error else 200)


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.pop("admin_id", None)
    session.pop("admin_name", None)
    session.pop("admin_username", None)
    flash("You have been signed out.", "info")
    return redirect(url_for("admin.login"))


def _claims_page(view_key):
    view = VIEWS[view_key]
    q = (request.args.get("q") or "").strip()[:80]
    status = request.args.get("status") or ""
    statuses = view["statuses"]
    if status in claim_service.STATUSES:
        statuses = [status]
    claims = claim_service.list_claims(q, statuses)
    return render_template("admin/dashboard.html", stats=claim_service.get_stats(), claims=claims,
                           q=q, status=status, statuses=claim_service.STATUSES,
                           view_key=view_key, view_title=view["title"],
                           show_cards=(view_key == "dashboard"))


@bp.route("/dashboard")
@login_required
def dashboard():
    return _claims_page("dashboard")


@bp.route("/claims")
@login_required
def claims():
    return _claims_page("all")


@bp.route("/claims/pending")
@login_required
def pending():
    return _claims_page("pending")


@bp.route("/claims/reviewed")
@login_required
def reviewed():
    return _claims_page("reviewed")


@bp.route("/claims/approved")
@login_required
def approved():
    return _claims_page("approved")


@bp.route("/claims/rejected")
@login_required
def rejected():
    return _claims_page("rejected")


@bp.route("/claim/<int:claim_id>")
@login_required
def claim_detail(claim_id):
    claim, files = claim_service.get_claim(claim_id)
    if not claim:
        abort(404)
    analysis = damage_detection_service.get_claim_analysis(get_db(), claim_id)
    return render_template("admin/claim_detail.html", claim=claim, files=files,
                           statuses=claim_service.STATUSES, fmt=policy_service.format_date,
                           analysis=analysis, money=estimation_service.format_money)


@bp.route("/claim/<int:claim_id>/chassis", methods=["POST"])
@login_required
def update_chassis(claim_id):
    """Claims officer confirms or corrects the chassis number read from the photo."""
    check_csrf()
    number = chassis_ocr_service.normalize_chassis(request.form.get("chassis_number"))
    if not 6 <= len(number) <= 25:
        flash("Enter the chassis number as shown on the plate (6–25 letters and digits).", "error")
    elif claim_service.update_chassis(claim_id, number, session.get("admin_name")):
        if chassis_ocr_service.is_valid_vin(number):
            flash(f"Chassis number {number} saved as verified.", "success")
        else:
            flash(f"Chassis number {number} saved. Note: it isn’t in the 17-character VIN format; "
                  "older vehicles can have shorter chassis numbers.", "info")
    else:
        abort(404)
    return redirect(url_for("admin.claim_detail", claim_id=claim_id) + "#chassis-number")


@bp.app_template_filter("chassis_status")
def chassis_status(status):
    return chassis_ocr_service.status_label(status)


@bp.route("/claim/<int:claim_id>/analyze", methods=["POST"])
@login_required
def analyze_claim(claim_id):
    """Run (or re-run) AI damage analysis for a claim in the background."""
    check_csrf()
    claim, _files = claim_service.get_claim(claim_id)
    if not claim:
        abort(404)
    ready, why = damage_detection_service.availability()
    if not ready:
        flash(f"AI damage analysis isn’t available: {why}", "error")
    else:
        damage_detection_service.start_background_analysis(
            current_app.config["DATABASE"], current_app.config["UPLOAD_FOLDER"], claim_id,
            wait=current_app.config.get("DAMAGE_ANALYSIS_SYNC", False),
            force_refresh=True)
        flash("AI damage analysis started. Results appear here in a moment.", "info")
    return redirect(url_for("admin.claim_detail", claim_id=claim_id) + "#damage-analysis")


@bp.route("/claim/<int:claim_id>/status", methods=["POST"])
@login_required
def update_status(claim_id):
    check_csrf()
    status = request.form.get("status")
    remarks = request.form.get("remarks", "")

    if status not in claim_service.STATUSES:
        flash("Please choose a valid status.", "error")
    elif claim_service.update_status(
        claim_id, status, session.get("admin_name"), remarks
    ):
        flash(f"Status updated to {status}.", "success")
    else:
        abort(404)

    return redirect(url_for("admin.claim_detail", claim_id=claim_id))


@bp.route("/file/<int:file_id>")
@login_required
def claim_file(file_id):
    """Serve evidence only to logged-in staff; the disk path is never exposed."""
    record = claim_service.get_file(file_id)
    if not record:
        abort(404)
    try:
        path = file_service.absolute_path(record["file_path"])
    except file_service.UploadError:
        abort(404)
    download = request.args.get("download") == "1"
    name = f"{record['file_type']}.{record['file_path'].rsplit('.', 1)[-1]}"
    return send_file(path, mimetype=record.get("mime_type") or file_service.mime_for(path),
                     conditional=True, as_attachment=download, download_name=name, max_age=0)


@bp.route("/damage-photo/<int:photo_id>")
@login_required
def damage_photo_file(photo_id):
    """Serve a damage photo to logged-in staff; disk path is never exposed."""
    record = claim_service.get_damage_photo(photo_id)
    if not record:
        abort(404)
    try:
        path = file_service.absolute_path(record["file_path"])
    except file_service.UploadError:
        abort(404)
    download = request.args.get("download") == "1"
    ext = record["file_path"].rsplit(".", 1)[-1]
    name = f"damage_photo_{photo_id}.{ext}"
    return send_file(path, mimetype=record.get("mime_type") or file_service.mime_for(path),
                     conditional=True, as_attachment=download, download_name=name, max_age=0)


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    db = get_db()
    if request.method == "POST":
        check_csrf()
        user = db.execute("SELECT * FROM admin_users WHERE id=?", (session["admin_id"],)).fetchone()
        current, new, confirm = (request.form.get(k) or "" for k in ("current", "new", "confirm"))
        if not check_password_hash(user["password_hash"], current):
            flash("Current password is incorrect.", "error")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif new != confirm:
            flash("New passwords do not match.", "error")
        else:
            db.execute("UPDATE admin_users SET password_hash=? WHERE id=?",
                       (generate_password_hash(new), user["id"]))
            flash("Password changed successfully.", "success")
        return redirect(url_for("admin.settings"))
    policies = [dict(r) for r in db.execute("SELECT * FROM policies ORDER BY policy_number")]
    for p in policies:
        p["eligible"], _, p["eligibility"] = policy_service.eligibility(p)
    return render_template("admin/settings.html", policies=policies, fmt=policy_service.format_date,
                           lan_ip=current_app.config.get("LAN_IP"), port=current_app.config.get("PORT"),
                           ai=validation_status())
