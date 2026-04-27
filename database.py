"""
SQLite database for PPIS Campus Agent.

Stores DVR configuration, camera mappings, and snapshot history
persistently. Replaces config.json for all mutable data.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger("ppis-agent.db")

DB_PATH = Path(__file__).parent / "ppis_agent.db"


def get_conn() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dvrs (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL DEFAULT '',
                ip       TEXT NOT NULL,
                port     INTEGER NOT NULL DEFAULT 80,
                username TEXT NOT NULL DEFAULT 'admin',
                password TEXT NOT NULL DEFAULT '',
                channels INTEGER NOT NULL DEFAULT 64
            );

            CREATE TABLE IF NOT EXISTS camera_mapping (
                location    TEXT PRIMARY KEY,
                dvr_index   INTEGER NOT NULL,
                channel     INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                all_cameras TEXT DEFAULT NULL  -- JSON array of camera entries
            );

            CREATE TABLE IF NOT EXISTS snapshot_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                classroom  TEXT NOT NULL,
                filename   TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                captured_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS registered_faces (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id  TEXT NOT NULL,
                name       TEXT NOT NULL,
                role       TEXT NOT NULL DEFAULT '',
                phone      TEXT NOT NULL DEFAULT '',
                angle      TEXT NOT NULL DEFAULT 'front',
                encoding   BLOB NOT NULL,
                image_path TEXT NOT NULL DEFAULT '',
                registered_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS attendance_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id    TEXT NOT NULL,
                name         TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'Present',
                confidence   REAL NOT NULL DEFAULT 0.0,
                snapshot_path TEXT NOT NULL DEFAULT '',
                camera_source TEXT NOT NULL DEFAULT '',
                logged_at    TEXT NOT NULL DEFAULT (datetime('now')),
                whatsapp_sent INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS attendance_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS unrecognized_faces (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_source TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                snapshot_path TEXT NOT NULL DEFAULT '',
                reviewed   INTEGER NOT NULL DEFAULT 0,
                detected_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn.commit()
        logger.info(f"Database initialized at {DB_PATH}")
    finally:
        conn.close()


def migrate_from_config_json(config_path: str | Path):
    """Migrate data from config.json into SQLite (one-time, on first run)."""
    config_path = Path(config_path)
    if not config_path.exists():
        logger.info("No config.json to migrate")
        return

    conn = get_conn()
    try:
        # Check if already migrated
        row = conn.execute(
            "SELECT value FROM settings WHERE key='migrated_from_json'"
        ).fetchone()
        if row:
            logger.info("Already migrated from config.json")
            return

        with open(config_path) as f:
            cfg = json.load(f)

        # Migrate settings
        for key in ("cloud_bot_url", "agent_secret", "local_port"):
            if key in cfg:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, str(cfg[key])),
                )

        # Migrate DVRs
        conn.execute("DELETE FROM dvrs")
        for dvr in cfg.get("dvrs", []):
            conn.execute(
                "INSERT INTO dvrs (name, ip, port, username, password, channels) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    dvr.get("name", ""),
                    dvr.get("ip", ""),
                    dvr.get("port", 80),
                    dvr.get("username", "admin"),
                    dvr.get("password", ""),
                    dvr.get("channels", 64),
                ),
            )

        # Migrate camera mapping
        conn.execute("DELETE FROM camera_mapping")
        for location, data in cfg.get("camera_mapping", {}).items():
            all_cameras = data.get("all_cameras")
            conn.execute(
                "INSERT OR REPLACE INTO camera_mapping "
                "(location, dvr_index, channel, description, all_cameras) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    location,
                    data.get("dvr_index", 0),
                    data.get("channel", 1),
                    data.get("description", ""),
                    json.dumps(all_cameras) if all_cameras else None,
                ),
            )

        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('migrated_from_json', '1')"
        )
        conn.commit()
        logger.info(
            f"Migrated from config.json: "
            f"{len(cfg.get('dvrs', []))} DVRs, "
            f"{len(cfg.get('camera_mapping', {}))} camera mappings"
        )
    except Exception as e:
        logger.error(f"Migration from config.json failed: {e}")
        conn.rollback()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_setting(key: str, value: str):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DVR helpers
# ---------------------------------------------------------------------------

def get_dvrs() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name, ip, port, username, password, channels FROM dvrs ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def save_dvrs(dvrs: list[dict]):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM dvrs")
        for dvr in dvrs:
            conn.execute(
                "INSERT INTO dvrs (name, ip, port, username, password, channels) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    dvr.get("name", ""),
                    dvr.get("ip", ""),
                    dvr.get("port", 80),
                    dvr.get("username", "admin"),
                    dvr.get("password", ""),
                    dvr.get("channels", 64),
                ),
            )
        conn.commit()
        logger.info(f"Saved {len(dvrs)} DVRs to database")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Camera mapping helpers
# ---------------------------------------------------------------------------

def get_camera_mapping() -> dict:
    """Return camera mapping as a dict (same structure as config.json)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT location, dvr_index, channel, description, all_cameras "
            "FROM camera_mapping"
        ).fetchall()
        mapping = {}
        for r in rows:
            entry = {
                "dvr_index": r["dvr_index"],
                "channel": r["channel"],
                "description": r["description"],
            }
            if r["all_cameras"]:
                entry["all_cameras"] = json.loads(r["all_cameras"])
            mapping[r["location"]] = entry
        return mapping
    finally:
        conn.close()


def save_camera_mapping(mapping: dict):
    """Save full camera mapping dict to database."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM camera_mapping")
        for location, data in mapping.items():
            all_cameras = data.get("all_cameras")
            conn.execute(
                "INSERT OR REPLACE INTO camera_mapping "
                "(location, dvr_index, channel, description, all_cameras) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    location,
                    data.get("dvr_index", 0),
                    data.get("channel", 1),
                    data.get("description", ""),
                    json.dumps(all_cameras) if all_cameras else None,
                ),
            )
        conn.commit()
        logger.info(f"Saved {len(mapping)} camera mappings to database")
    finally:
        conn.close()


def get_camera_count() -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM camera_mapping").fetchone()
        return row["cnt"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Snapshot history
# ---------------------------------------------------------------------------

def log_snapshot(classroom: str, filename: str, size_bytes: int):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO snapshot_history (classroom, filename, size_bytes) "
            "VALUES (?, ?, ?)",
            (classroom, filename, size_bytes),
        )
        conn.commit()
    finally:
        conn.close()


def get_snapshot_history(limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT classroom, filename, size_bytes, captured_at "
            "FROM snapshot_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Full config loader (drop-in replacement for load_config)
# ---------------------------------------------------------------------------

def load_config_from_db() -> dict:
    """Load full config from database (same structure as config.json)."""
    return {
        "cloud_bot_url": get_setting("cloud_bot_url", "wss://app-itszlsnn.fly.dev/ws/agent"),
        "agent_secret": get_setting("agent_secret", os.environ.get("AGENT_SECRET", "")),
        "local_port": int(get_setting("local_port", "8897")),
        "dvrs": get_dvrs(),
        "camera_mapping": get_camera_mapping(),
    }


# ---------------------------------------------------------------------------
# Registered faces helpers
# ---------------------------------------------------------------------------

def save_face_encoding(person_id: str, name: str, role: str, phone: str,
                       angle: str, encoding_bytes: bytes, image_path: str) -> int:
    conn = get_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO registered_faces "
            "(person_id, name, role, phone, angle, encoding, image_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (person_id, name, role, phone, angle, encoding_bytes, image_path),
        )
        conn.commit()
        logger.info(f"Saved face encoding for {name} ({person_id}), angle={angle}")
        return cursor.lastrowid
    finally:
        conn.close()


def get_all_face_encodings() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, person_id, name, role, phone, angle, encoding, image_path "
            "FROM registered_faces ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_registered_persons() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT person_id, name, role, phone, "
            "COUNT(*) as face_count, "
            "GROUP_CONCAT(angle) as angles, "
            "MIN(registered_at) as registered_at "
            "FROM registered_faces GROUP BY person_id ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_person_faces(person_id: str) -> int:
    conn = get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM registered_faces WHERE person_id = ?", (person_id,)
        )
        conn.commit()
        deleted = cursor.rowcount
        logger.info(f"Deleted {deleted} face encoding(s) for person_id={person_id}")
        return deleted
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Attendance log helpers
# ---------------------------------------------------------------------------

def log_attendance(person_id: str, name: str, status: str, confidence: float,
                   snapshot_path: str, camera_source: str) -> int:
    conn = get_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO attendance_log "
            "(person_id, name, status, confidence, snapshot_path, camera_source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, name, status, confidence, snapshot_path, camera_source),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_whatsapp_sent(attendance_id: int):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE attendance_log SET whatsapp_sent = 1 WHERE id = ?",
            (attendance_id,),
        )
        conn.commit()
    finally:
        conn.close()


def get_attendance_log(limit: int = 100, person_id: str | None = None) -> list[dict]:
    conn = get_conn()
    try:
        if person_id:
            rows = conn.execute(
                "SELECT id, person_id, name, status, confidence, snapshot_path, "
                "camera_source, logged_at, whatsapp_sent "
                "FROM attendance_log WHERE person_id = ? ORDER BY id DESC LIMIT ?",
                (person_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, person_id, name, status, confidence, snapshot_path, "
                "camera_source, logged_at, whatsapp_sent "
                "FROM attendance_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_last_attendance(person_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, person_id, name, status, confidence, logged_at "
            "FROM attendance_log WHERE person_id = ? ORDER BY id DESC LIMIT 1",
            (person_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def is_attendance_marked_today(person_id: str) -> bool:
    """Check if a student already has attendance for today."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM attendance_log "
            "WHERE person_id = ? AND date(logged_at) = date('now') LIMIT 1",
            (person_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_today_attendance_count() -> int:
    """Get count of unique students marked today."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT person_id) as cnt "
            "FROM attendance_log WHERE date(logged_at) = date('now')"
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def get_today_attendance_summary() -> list[dict]:
    """Get attendance summary for today grouped by camera/classroom."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT camera_source, COUNT(DISTINCT person_id) as student_count, "
            "MIN(logged_at) as first_at, MAX(logged_at) as last_at "
            "FROM attendance_log WHERE date(logged_at) = date('now') "
            "GROUP BY camera_source ORDER BY student_count DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def log_unrecognized_face(camera_source: str, confidence: float,
                          snapshot_path: str) -> int:
    conn = get_conn()
    try:
        cursor = conn.execute(
            "INSERT INTO unrecognized_faces "
            "(camera_source, confidence, snapshot_path) VALUES (?, ?, ?)",
            (camera_source, confidence, snapshot_path),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_unrecognized_faces(limit: int = 50, unreviewed_only: bool = True) -> list[dict]:
    conn = get_conn()
    try:
        if unreviewed_only:
            rows = conn.execute(
                "SELECT id, camera_source, confidence, snapshot_path, reviewed, detected_at "
                "FROM unrecognized_faces WHERE reviewed = 0 ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, camera_source, confidence, snapshot_path, reviewed, detected_at "
                "FROM unrecognized_faces ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_attendance_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM attendance_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def set_attendance_setting(key: str, value: str):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO attendance_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
