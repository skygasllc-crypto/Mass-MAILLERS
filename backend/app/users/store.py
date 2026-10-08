"""User accounts: registration, password hashing, admin management."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import shutil

from ..config import settings
from ..db import claim_legacy_data, get_db, now_iso
from ..recipients.parser import is_valid_email

log = logging.getLogger("mailer.users")

ROLES = {"admin", "user"}
STATUSES = {"active", "pending", "disabled"}
MIN_PASSWORD = 8
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class UserError(ValueError):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), dklen=32, **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # equalizes timing for unknown emails


def public(row) -> dict:
    return {k: row[k] for k in ("id", "email", "role", "status", "created_at", "last_login")}


def get(user_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def authenticate(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if row is None:
        verify_password(password, _DUMMY_HASH)
        raise UserError("Incorrect email or password.")
    if not verify_password(password, row["password_hash"]):
        raise UserError("Incorrect email or password.")
    if row["status"] == "pending":
        raise UserError("Your account is waiting for approval by the administrator.")
    if row["status"] == "disabled":
        raise UserError("Your account has been disabled. Contact the administrator.")
    with get_db() as conn:
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now_iso(), row["id"]))
    return dict(row)


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise UserError(f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > 200:
        raise UserError("Password is too long.")


def register(email: str, password: str) -> dict:
    if not settings.allow_registration:
        raise UserError("Registration is closed.")
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        raise UserError("Enter a valid email address.")
    _check_password(password)
    status = "pending" if settings.registration_requires_approval else "active"
    with get_db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise UserError("An account with this email already exists. Sign in instead.")
        cur = conn.execute(
            "INSERT INTO users(email, password_hash, role, status, created_at) VALUES(?,?,?,?,?)",
            (email, hash_password(password), "user", status, now_iso()),
        )
        user_id = cur.lastrowid
    log.info("New account registered (id %s, %s)", user_id, status)
    return get(user_id)


def change_password(user_id: int, current: str, new: str) -> None:
    user = get(user_id)
    if user is None or not verify_password(current, user["password_hash"]):
        raise UserError("Current password is incorrect.")
    if user["email"] == settings.admin_email:
        raise UserError("The main admin password is set with ADMIN_PASSWORD in the .env file.")
    _check_password(new)
    with get_db() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new), user_id))


def list_users() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT u.*, (SELECT COUNT(*) FROM jobs j WHERE j.user_id=u.id) AS sends, "
            "(SELECT COUNT(*) FROM job_recipients r JOIN jobs j ON j.id=r.job_id "
            " WHERE j.user_id=u.id AND r.status='sent') AS emails_sent "
            "FROM users u ORDER BY u.role='admin' DESC, u.created_at"
        ).fetchall()
    return [{**public(r), "sends": r["sends"], "emails_sent": r["emails_sent"],
             "main_admin": r["email"] == settings.admin_email} for r in rows]


def _guard(actor_id: int, target: dict) -> None:
    if target["id"] == actor_id:
        raise UserError("You cannot change your own account here.")
    if target["email"] == settings.admin_email:
        raise UserError("The main admin account (from .env) cannot be changed.")


def set_status(actor_id: int, user_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise UserError("Unknown status.")
    target = get(user_id)
    if target is None:
        raise UserError("User not found.")
    _guard(actor_id, target)
    with get_db() as conn:
        conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
    log.info("User %s set to %s by admin %s", user_id, status, actor_id)
    return get(user_id)


def delete(actor_id: int, user_id: int) -> None:
    """Delete an account and everything it owns (settings, attachments, history, templates)."""
    target = get(user_id)
    if target is None:
        raise UserError("User not found.")
    _guard(actor_id, target)
    with get_db() as conn:
        files = [r["stored_name"] for r in conn.execute("SELECT stored_name FROM attachments WHERE user_id=?", (user_id,))]
        conn.execute("DELETE FROM job_recipients WHERE job_id IN (SELECT id FROM jobs WHERE user_id=?)", (user_id,))
        for table in ("jobs", "attachments", "dev_outbox", "user_settings", "suppressions", "smtp_profiles"):
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    for name in files:
        (settings.upload_dir / name).unlink(missing_ok=True)
    shutil.rmtree(settings.templates_dir / str(user_id), ignore_errors=True)
    log.info("User %s deleted by admin %s", user_id, actor_id)


def ensure_admin() -> dict:
    """Create/refresh the main admin account from ADMIN_EMAIL / ADMIN_PASSWORD."""
    email, password = settings.admin_email, settings.admin_password
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(email, password_hash, role, status, created_at) VALUES(?,?,?,?,?)",
                (email, hash_password(password), "admin", "active", now_iso()),
            )
        elif not verify_password(password, row["password_hash"]) or row["role"] != "admin" or row["status"] != "active":
            conn.execute("UPDATE users SET password_hash=?, role='admin', status='active' WHERE email=?",
                         (hash_password(password), email))
        admin = dict(conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone())
    claim_legacy_data(admin["id"])
    _claim_legacy_templates(admin["id"])
    return admin


def _claim_legacy_templates(user_id: int) -> None:
    """Templates saved before accounts existed lived directly in templates/<id>/."""
    root = settings.templates_dir
    if not root.exists():
        return
    for folder in root.iterdir():
        if folder.is_dir() and len(folder.name) == 12 and (folder / "template.json").exists():
            dest = root / str(user_id) / folder.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(dest))
