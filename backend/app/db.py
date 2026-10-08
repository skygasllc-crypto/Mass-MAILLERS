"""SQLite storage: users, per-user settings, attachments, jobs, results, do-not-send list, dev mailbox."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import settings

_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    last_login    TEXT
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER NOT NULL,
    key     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS smtp_profiles (  -- saved SMTP accounts the user can switch between
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    data_enc    TEXT NOT NULL,  -- encrypted settings incl. password
    verified_fp TEXT,           -- fingerprint of the settings when they last passed the SMTP test
    updated_at  TEXT NOT NULL,
    UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS suppressions (
    user_id    INTEGER NOT NULL,
    email      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, email)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS attachments (
    id           TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    stored_name  TEXT NOT NULL,
    size         INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    status_text   TEXT,
    subject       TEXT,
    from_email    TEXT,
    reply_to      TEXT,
    email_mode    TEXT,
    total         INTEGER NOT NULL,
    batch_size    INTEGER NOT NULL,
    total_batches INTEGER NOT NULL,
    current_batch INTEGER NOT NULL DEFAULT 0,
    speed         TEXT,
    payload       TEXT NOT NULL,
    smtp_account  TEXT,  -- "username @ host" shown in the UI
    smtp_enc      TEXT   -- encrypted SMTP settings the job was started with; cleared when it ends
);
CREATE TABLE IF NOT EXISTS job_recipients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    email         TEXT NOT NULL,
    name          TEXT,
    batch_no      INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    smtp_response TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_recipients_job ON job_recipients(job_id, status);
-- legacy single-user tables, migrated to the first admin by claim_legacy_data()
CREATE TABLE IF NOT EXISTS suppression (
    email      TEXT PRIMARY KEY,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dev_outbox (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    envelope_from  TEXT NOT NULL,
    envelope_to    TEXT NOT NULL,
    subject        TEXT,
    raw            TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    """Yield a connection; commits on success. Writes are serialized with a lock."""
    with _write_lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


OWNED_TABLES = ("attachments", "jobs", "dev_outbox")


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)
        for table in OWNED_TABLES:  # upgrade databases created before user accounts existed
            columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if "user_id" not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
        job_columns = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        for column in ("smtp_account", "smtp_enc"):
            if column not in job_columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at)")
        # A job that was running when the process stopped cannot resume safely.
        conn.execute(
            "UPDATE jobs SET status='interrupted', status_text='Stopped: application was restarted', "
            "finished_at=?, smtp_enc=NULL WHERE status IN ('queued', 'running')",
            (now_iso(),),
        )
        conn.execute(
            "UPDATE job_recipients SET status='not_sent', smtp_response='Not sent (interrupted)' "
            "WHERE status='pending' AND job_id IN (SELECT id FROM jobs WHERE status='interrupted')"
        )


def claim_legacy_data(user_id: int) -> None:
    """Give data from the single-user version (no owner) to ``user_id`` (the first admin)."""
    with get_db() as conn:
        for table in OWNED_TABLES:
            conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id IS NULL", (user_id,))
        has_settings = conn.execute("SELECT 1 FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        if not has_settings:
            conn.execute("INSERT INTO user_settings(user_id, key, value) SELECT ?, key, value FROM settings", (user_id,))
        conn.execute("INSERT OR IGNORE INTO suppressions(user_id, email, reason, created_at) "
                     "SELECT ?, email, reason, created_at FROM suppression", (user_id,))
        conn.execute("DELETE FROM settings")
        conn.execute("DELETE FROM suppression")


def get_setting(user_id: int, key: str, default: str | None = None) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)).fetchone()
    return row["value"] if row else default


def set_settings(user_id: int, values: dict[str, str | None]) -> None:
    with get_db() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO user_settings(user_id, key, value) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
                (user_id, key, value),
            )
