"""SQLite schema and connection helpers.

All SQL lives here and in the services layer, so swapping SQLite for a real
insurance database later only means changing this module and the services.
"""
import os
import sqlite3

from flask import current_app, g

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "instance", "insurance.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS policies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_number     TEXT    NOT NULL UNIQUE,
    customer_name     TEXT    NOT NULL,
    customer_phone    TEXT    NOT NULL,
    vehicle_number    TEXT    NOT NULL,
    vehicle_make      TEXT    NOT NULL,
    vehicle_model     TEXT    NOT NULL,
    policy_status     TEXT    NOT NULL DEFAULT 'Active',   -- Active | Inactive | Cancelled
    policy_start_date TEXT    NOT NULL,                    -- YYYY-MM-DD
    policy_end_date   TEXT    NOT NULL,                    -- YYYY-MM-DD
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS claims (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_reference  TEXT    NOT NULL UNIQUE,
    policy_id        INTEGER NOT NULL REFERENCES policies(id) ON DELETE RESTRICT,
    policy_number    TEXT    NOT NULL,
    customer_name    TEXT    NOT NULL,
    vehicle_number   TEXT    NOT NULL,
    submitted_at     TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'Pending Review',
    reviewed_by      TEXT,
    chassis_number            TEXT,   -- extracted from the chassis photo (or entered by the claims team)
    chassis_ocr_status        TEXT,   -- VERIFIED, MANUAL_REVIEW, NOT_DETECTED, MANUALLY_VERIFIED
    chassis_ocr_confidence    REAL,
    chassis_original_ocr_text TEXT,   -- JSON audit: raw OCR lines, candidates, engine number, corrections
    chassis_verified_by       TEXT,
    ocr_vehicle_number        TEXT,   -- vehicle registration number read from photo via OCR
    ocr_vehicle_status        TEXT,   -- VERIFIED, MANUAL_REVIEW, NOT_DETECTED
    ocr_vehicle_confidence    REAL,
    ocr_vehicle_raw_text      TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_policy ON claims(policy_id);

CREATE TABLE IF NOT EXISTS claim_files (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id          INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    file_type         TEXT    NOT NULL,   -- rear_photo, back_photo, left_photo, right_photo, damage_video, chassis_photo
    file_path         TEXT    NOT NULL,   -- relative to uploads/ (never shown to customers)
    original_filename TEXT,
    mime_type         TEXT,
    file_size         INTEGER,
    uploaded_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    -- AI photo validation (NULL for the damage video)
    expected_view       TEXT,             -- REAR, LEFT_SIDE, RIGHT_SIDE, CHASSIS
    detected_view       TEXT,             -- FRONT, REAR, LEFT_SIDE, RIGHT_SIDE, CHASSIS, UNKNOWN
    confidence          REAL,             -- 0..1 from the vision model
    validated_at        TEXT,
    validation_status   TEXT,             -- ACCEPTED or NOT_VERIFIED
    validation_details  TEXT,             -- JSON: model notes, VIN text read, quality metrics
    UNIQUE (claim_id, file_type)
);

-- AI damage analysis (one row per detected damage area, per photo). Original photos are never modified.
CREATE TABLE IF NOT EXISTS damage_assessments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id        INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    claim_file_id   INTEGER NOT NULL REFERENCES claim_files(id) ON DELETE CASCADE,
    damage_type     TEXT    NOT NULL,     -- SCRATCH, DENT, CRACK, PAINT_DAMAGE, BUMPER_DAMAGE, GLASS_DAMAGE, OTHER
    vehicle_part    TEXT,                 -- e.g. REAR_BUMPER, LEFT_FRONT_DOOR
    location        TEXT,                 -- human description from the model
    severity        TEXT    NOT NULL,     -- MINOR, MODERATE, SEVERE, UNCERTAIN
    confidence      REAL    NOT NULL,     -- 0..1
    review_status   TEXT    NOT NULL,     -- CONFIRMED (>= threshold) or POSSIBLE (needs manual review)
    is_primary      INTEGER NOT NULL DEFAULT 1,  -- 0 when the same damage was already counted from another photo
    estimated_cost  INTEGER,              -- NULL when not priced (possible / uncertain severity)
    bounding_box    TEXT    NOT NULL,     -- JSON {"x","y","w","h"} as fractions of the image
    reason          TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_damage_claim ON damage_assessments(claim_id);

-- Repair estimate + analysis status per claim.
CREATE TABLE IF NOT EXISTS claim_estimations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id             INTEGER NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
    analysis_status      TEXT    NOT NULL,   -- PENDING, COMPLETE, FAILED, UNAVAILABLE
    analysis_message     TEXT,
    provider             TEXT,
    model                TEXT,
    parts_cost           INTEGER,
    labour_cost          INTEGER,
    paint_cost           INTEGER,
    other_cost           INTEGER,
    total_estimated_cost INTEGER,
    currency             TEXT,
    photo_results        TEXT,               -- JSON per photo: analyzed / failed / notes
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS claim_damage_photos (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id           INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    file_path          TEXT    NOT NULL,
    original_filename  TEXT,
    mime_type          TEXT,
    file_size          INTEGER,
    uploaded_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    damage_type        TEXT,     -- Scratch, Dent, Crack, Paint damage, Other/uncertain
    confidence         REAL,     -- 0..1 only if genuine from model
    analysis_status    TEXT,     -- COMPLETE, UNAVAILABLE, MANUAL_REVIEW
    analysis_details   TEXT
);
CREATE INDEX IF NOT EXISTS idx_damage_photos_claim ON claim_damage_photos(claim_id);

CREATE TABLE IF NOT EXISTS admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    full_name     TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path=None):
    """Open a standalone connection (used by scripts and seeding)."""
    db_path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # isolation_level=None: autocommit by default; multi-step writes use explicit BEGIN IMMEDIATE
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """Per-request connection stored on flask.g."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added after the first release; existing databases are upgraded in place.
MIGRATIONS = {
    "claims": [
        ("chassis_number", "TEXT"), ("chassis_ocr_status", "TEXT"), ("chassis_ocr_confidence", "REAL"),
        ("chassis_original_ocr_text", "TEXT"), ("chassis_verified_by", "TEXT"),
        ("ocr_vehicle_number", "TEXT"), ("ocr_vehicle_status", "TEXT"), ("ocr_vehicle_confidence", "REAL"),
        ("ocr_vehicle_raw_text", "TEXT"),("mobile", "TEXT"),("email", "TEXT"),("review_remarks", "TEXT"),
    ],
    "claim_files": [
        ("expected_view", "TEXT"), ("detected_view", "TEXT"), ("confidence", "REAL"),
        ("validated_at", "TEXT"), ("validation_status", "TEXT"), ("validation_details", "TEXT"),
    ],
}


def _migrate(conn):
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
    conn.execute("""CREATE TABLE IF NOT EXISTS claim_damage_photos (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id           INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
        file_path          TEXT    NOT NULL,
        original_filename  TEXT,
        mime_type          TEXT,
        file_size          INTEGER,
        uploaded_at        TEXT    NOT NULL DEFAULT (datetime('now')),
        damage_type        TEXT,
        confidence         REAL,
        analysis_status    TEXT,
        analysis_details   TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_damage_photos_claim ON claim_damage_photos(claim_id)")


def init_db(db_path=None):
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
