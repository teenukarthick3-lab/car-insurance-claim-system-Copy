"""Create tables and seed demo policies + the demo admin account.

Run directly:   python -m database.seed          (safe to run repeatedly)
Reset all data: python -m database.seed --reset  (deletes DB and uploaded files)
"""
import os
import shutil
import sys
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.models import BASE_DIR, DEFAULT_DB_PATH, connect, init_db  # noqa: E402

DEMO_ADMIN = {"username": "admin", "password": "Admin@123", "full_name": "Claims Officer"}


def _demo_policies():
    """Dates are relative to today so the demo stays valid whenever it is run."""
    t = date.today()
    d = lambda days: (t + timedelta(days=days)).isoformat()  # noqa: E731
    return [
        # policy, name, phone, reg no, make, model, status, start, end
        ("POL10001", "Teenu Karthick",   "+91 98430 12345", "TN38CT5038", "Maruti Suzuk",       "Wagnor",   "Active",   d(-120), d(245)),
        ("POL10002", "Arjun Ramesh",     "+91 94432 67890", "TN37CK5678", "Maruti Suzuki", "Swift",   "Active",   d(-30),  d(335)),
        ("POL10003", "Priya Venkatesh",  "+91 99520 24680", "TN39BZ9012", "Tata",          "Nexon",   "Active",   d(-200), d(165)),
        ("POL10004", "Mohammed Farhan",  "+91 97890 13579", "KA05MN3456", "Honda",         "City",    "Active",   d(-400), d(-35)),   # expired by date
        ("POL10005", "Lakshmi Narayanan","+91 90030 11223", "TN66DE7890", "Mahindra",      "XUV700",  "Inactive", d(-90),  d(275)),   # lapsed / inactive
        ("POL10006", "Divya Shankar",    "+91 98840 44556", "TN38CQ4321", "Kia",           "Seltos",  "Active",   d(-10),  d(355)),
    ]


def seed(db_path=None, verbose=True):
    init_db(db_path)
    conn = connect(db_path)
    added = 0
    for row in _demo_policies():
        cur = conn.execute(
            """INSERT OR IGNORE INTO policies
               (policy_number, customer_name, customer_phone, vehicle_number, vehicle_make,
                vehicle_model, policy_status, policy_start_date, policy_end_date)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            row,
        )
        added += cur.rowcount
    if not conn.execute("SELECT 1 FROM admin_users WHERE username=?", (DEMO_ADMIN["username"],)).fetchone():
        conn.execute(
            "INSERT INTO admin_users (username, full_name, password_hash) VALUES (?,?,?)",
            (DEMO_ADMIN["username"], DEMO_ADMIN["full_name"], generate_password_hash(DEMO_ADMIN["password"])),
        )
        if verbose:
            print(f"Created demo admin: {DEMO_ADMIN['username']} / {DEMO_ADMIN['password']}")
    conn.commit()
    conn.close()
    if verbose:
        print(f"Database ready at {db_path or DEFAULT_DB_PATH} ({added} new demo policies).")


def reset(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    if os.path.exists(path):
        os.remove(path)
    claims_dir = os.path.join(BASE_DIR, "uploads", "claims")
    if os.path.isdir(claims_dir):
        shutil.rmtree(claims_dir)
    os.makedirs(claims_dir, exist_ok=True)
    print("Database and uploaded claim files removed.")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    seed()
