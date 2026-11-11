"""Car Insure — Car Insurance Claim Management System.

Run:  python app.py
Listens on 0.0.0.0:5000 so phones on the same Wi-Fi can reach it.
"""
import logging
import os
import secrets
import socket
import sys
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request, url_for

import config  # noqa: F401  (loads .env before anything reads settings)
from database import models
from database.seed import seed
from routes.admin import bp as admin_bp
from routes.customer import bp as customer_bp
from security import csrf_token

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))


def _secret_key(instance_dir):
    """Stable random secret stored in instance/ (override with SECRET_KEY env var)."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(instance_dir, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def get_lan_ip():
    """Detect the laptop's Wi-Fi/LAN address dynamically (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        ip = None
    finally:
        s.close()
    if not ip or ip.startswith("127."):
        try:
            candidates = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
            ip = next((c for c in candidates if not c.startswith("127.")), None)
        except OSError:
            ip = None
    return ip


def create_app(test_config=None):
    instance_dir = os.path.join(BASE_DIR, "instance")
    upload_dir = os.path.join(BASE_DIR, "uploads")
    os.makedirs(instance_dir, exist_ok=True)
    os.makedirs(os.path.join(upload_dir, "claims"), exist_ok=True)

    app = Flask(__name__, instance_path=instance_dir)
    app.config.update(
        SECRET_KEY=_secret_key(instance_dir),
        DATABASE=os.path.join(instance_dir, "insurance.db"),
        UPLOAD_FOLDER=upload_dir,
        MAX_CONTENT_LENGTH=200 * 1024 * 1024,     # whole request: 5 photos + 1 video
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        LAN_IP=get_lan_ip(),
        PORT=PORT,
    )
    if test_config:
        app.config.update(test_config)

    models.init_app(app)
    seed(app.config["DATABASE"], verbose=False)   # creates tables + demo data if missing

    app.register_blueprint(customer_bp)
    app.register_blueprint(admin_bp)
    app.jinja_env.globals["csrf_token"] = csrf_token

    photo_dir = os.path.join(app.static_folder, "images", "photos")

    def hero_style(name):
        """CSS variables for a hero background: your own photo (if added) over the bundled artwork.

        Drop e.g. static/images/photos/hero.jpg (or login.jpg / inspection.jpg / success.jpg)
        to replace the illustration with a real photograph. No code change needed.
        """
        art = url_for("static", filename="images/hero-art.svg")
        style = f"--hero-art:url('{art}');"
        for ext in ("jpg", "jpeg", "webp", "png"):
            if os.path.exists(os.path.join(photo_dir, f"{name}.{ext}")):
                style += f"--hero-photo:url('{url_for('static', filename=f'images/photos/{name}.{ext}')}');"
                break
        return style

    @app.context_processor
    def brand_globals():
        return {"hero_style": hero_style, "now_year": datetime.now().year}

    @app.route("/favicon.ico")
    def favicon():
        return app.send_static_file("images/logo.svg")

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.path.startswith("/admin"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def _error(code, title, text):
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(ok=False, error=text), code
        area = "admin" if request.path.startswith("/admin") else "customer"
        return render_template(f"{area}/error.html", code=code, title=title, text=text), code

    @app.errorhandler(400)
    def bad_request(e):
        return _error(400, "Something didn't look right", getattr(e, "description", None) or "Please try again.")

    @app.errorhandler(404)
    def not_found(_e):
        return _error(404, "Page not found", "The page you're looking for doesn't exist or has moved.")

    @app.errorhandler(413)
    def too_large(_e):
        return _error(413, "Files too large",
                      "Your files are too large to upload together. Please use smaller photos or a shorter video (max 100 MB).")

    @app.errorhandler(500)
    def server_error(_e):
        return _error(500, "Something went wrong", "We hit a problem on our side. Please try again in a moment.")

    return app


def print_banner(lan_ip):
    line = "=" * 48
    phone = f"http://{lan_ip}:{PORT}" if lan_ip else None
    print(f"\n{line}\n🚗 CAR INSURANCE CLAIM SYSTEM\n{line}\n")
    print("Server started successfully.\n")
    print(f"💻 Computer:\nhttp://127.0.0.1:{PORT}/claim\n")
    if phone:
        print(f"📱 CUSTOMER PHONE:\n{phone}/claim\n")
        print(f"💻 ADMIN:\n{phone}/admin/login\n")
    else:
        print("📱 CUSTOMER PHONE:\nCould not detect a Wi-Fi/LAN address.\n"
              "Connect to Wi-Fi and restart, or find your IP with ipconfig / ifconfig.\n")
        print(f"💻 ADMIN:\nhttp://127.0.0.1:{PORT}/admin/login\n")
    print("Demo admin login:  admin / Admin@123")
    print("Demo policy:       POL10001\n")
    from services.vehicle_image_validator import validation_status
    ai = validation_status()
    print(f"🤖 AI PHOTO CHECK: {ai['summary']}")
    if ai["active"] and not ai["ready"]:
        print("   → Put GEMINI_API_KEY=... in a .env file next to app.py and restart,")
        print("     or set PHOTO_VALIDATION_MODE=auto to accept photos without AI. See README.\n")
    elif not ai["ready"] and ai["mode"] != "off":
        print("   → To turn the AI check on: put GEMINI_API_KEY=... in a .env file next to app.py")
        print("     (copy .env.example) and restart. See README.\n")
    else:
        print()
    print("Make sure the phone and laptop\nare connected to the same Wi-Fi.")
    print("(127.0.0.1 only works on this laptop itself.)")
    print(f"{line}\n")


if __name__ == "__main__":
    # Windows consoles may not be UTF-8; never crash on the emoji banner.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    application = create_app()
    print_banner(application.config["LAN_IP"])
    application.run(host=HOST, port=PORT, debug=False, threaded=True)
