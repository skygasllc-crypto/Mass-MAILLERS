"""Persisted SMTP settings and sender identity. The password is stored encrypted."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from ..db import get_db, get_setting, set_settings, now_iso
from ..utils.crypto import decrypt, encrypt, fingerprint

SECURITY_OPTIONS = {"starttls", "ssl", "none"}
_KEYS = ["smtp_host", "smtp_port", "smtp_security", "smtp_username", "from_name", "from_email", "reply_to", "to_address",
         "unsubscribe_email", "unsubscribe_url", "company_address", "add_footer"]


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    security: str = "starttls"
    username: str = ""
    password: str = ""
    from_name: str = ""
    from_email: str = ""
    reply_to: str = ""
    to_address: str = ""  # visible "To:" header; defaults to from_email
    unsubscribe_email: str = ""  # List-Unsubscribe mailto; defaults to reply_to / from_email
    unsubscribe_url: str = ""  # optional https:// one-click unsubscribe endpoint (RFC 8058)
    company_address: str = ""  # postal address shown in the footer
    add_footer: bool = False
    user_id: int = 0  # owner; not stored as a setting
    trusted: bool = False  # admins may use local/private SMTP hosts and any port

    @property
    def visible_to(self) -> str:
        return self.to_address or self.from_email

    @property
    def unsubscribe_mailto(self) -> str:
        return self.unsubscribe_email or self.reply_to or self.from_email

    @property
    def account(self) -> str:
        """Label for the SMTP account, e.g. ``news@example.com @ smtp.example.com``."""
        return f"{self.username or self.from_email} @ {self.host}" if self.host else self.from_email

    @property
    def account_key(self) -> str:
        """Identifies the SMTP account: only one send may use it at a time."""
        return fingerprint("account", self.host.lower(), str(self.port), (self.username or self.from_email).lower())

    def fingerprint(self) -> str:
        return fingerprint(self.host.lower(), str(self.port), self.security, self.username, self.password)

    def public(self) -> dict:
        """Settings safe to send to the browser: never includes the password."""
        verified = is_verified(self)
        return {
            "host": self.host,
            "port": self.port,
            "security": self.security,
            "username": self.username,
            "password_set": bool(self.password),
            "from_name": self.from_name,
            "from_email": self.from_email,
            "reply_to": self.reply_to,
            "to_address": self.to_address,
            "unsubscribe_email": self.unsubscribe_email,
            "unsubscribe_url": self.unsubscribe_url,
            "company_address": self.company_address,
            "add_footer": self.add_footer,
            "verified": verified,
            "verified_at": get_setting(self.user_id, "verified_at") if verified else None,
        }


def load(user_id: int) -> SmtpConfig:
    values = {k: get_setting(user_id, k, "") or "" for k in _KEYS}
    port = values["smtp_port"]
    return SmtpConfig(
        host=values["smtp_host"],
        port=int(port) if port.isdigit() else 587,
        security=values["smtp_security"] or "starttls",
        username=values["smtp_username"],
        password=decrypt(get_setting(user_id, "smtp_password_enc")),
        from_name=values["from_name"],
        from_email=values["from_email"],
        reply_to=values["reply_to"],
        to_address=values["to_address"],
        unsubscribe_email=values["unsubscribe_email"],
        unsubscribe_url=values["unsubscribe_url"],
        company_address=values["company_address"],
        add_footer=values["add_footer"] == "1",
        user_id=user_id,
        trusted=_is_admin(user_id),
    )


def snapshot(cfg: SmtpConfig) -> str:
    """Encrypted copy of the settings, so a running send is unaffected by later changes."""
    return encrypt(json.dumps(asdict(cfg)))


def from_snapshot(token: str) -> SmtpConfig | None:
    data = decrypt(token)
    return SmtpConfig(**json.loads(data)) if data else None


def _is_admin(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row) and row["role"] == "admin"


def save(user_id: int, cfg: SmtpConfig, new_password: str | None) -> None:
    """Persist settings. ``new_password=None`` keeps the stored password."""
    values = {
        "smtp_host": cfg.host,
        "smtp_port": str(cfg.port),
        "smtp_security": cfg.security,
        "smtp_username": cfg.username,
        "from_name": cfg.from_name,
        "from_email": cfg.from_email,
        "reply_to": cfg.reply_to,
        "to_address": cfg.to_address,
        "unsubscribe_email": cfg.unsubscribe_email,
        "unsubscribe_url": cfg.unsubscribe_url,
        "company_address": cfg.company_address,
        "add_footer": "1" if cfg.add_footer else "0",
    }
    if new_password is not None:
        values["smtp_password_enc"] = encrypt(new_password) if new_password else ""
    set_settings(user_id, values)


def mark_verified(cfg: SmtpConfig) -> None:
    set_settings(cfg.user_id, {"verified_fingerprint": cfg.fingerprint(), "verified_at": now_iso()})


def is_verified(cfg: SmtpConfig) -> bool:
    return bool(cfg.host) and get_setting(cfg.user_id, "verified_fingerprint") == cfg.fingerprint()


# --------------------------------------------------------------------------- saved SMTP accounts

MAX_PROFILES = 50


class ProfileError(Exception):
    pass


def list_profiles(user_id: int) -> list[dict]:
    """Saved accounts for the account picker; never includes the password."""
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, data_enc, updated_at FROM smtp_profiles WHERE user_id=? "
                            "ORDER BY name COLLATE NOCASE", (user_id,)).fetchall()
    out = []
    for r in rows:
        cfg = from_snapshot(r["data_enc"])
        out.append({"id": r["id"], "name": r["name"], "updated_at": r["updated_at"],
                    "host": cfg.host if cfg else "", "username": cfg.username if cfg else "",
                    "from_email": cfg.from_email if cfg else "", "readable": cfg is not None})
    return out


def save_profile(user_id: int, name: str) -> dict:
    """Save the current SMTP settings (including the password) under ``name``; replaces a same-named one."""
    name = " ".join(name.split())
    if not name:
        raise ProfileError("Enter a name for this SMTP account.")
    cfg = load(user_id)
    if not cfg.host:
        raise ProfileError("Enter the SMTP settings first.")
    verified_fp = cfg.fingerprint() if is_verified(cfg) else None
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM smtp_profiles WHERE user_id=? AND name=?", (user_id, name)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM smtp_profiles WHERE user_id=?", (user_id,)).fetchone()[0]
        if not exists and count >= MAX_PROFILES:
            raise ProfileError(f"You can save at most {MAX_PROFILES} SMTP accounts. Delete one first.")
        conn.execute(
            "INSERT INTO smtp_profiles(user_id, name, data_enc, verified_fp, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id, name) DO UPDATE SET data_enc=excluded.data_enc, "
            "verified_fp=excluded.verified_fp, updated_at=excluded.updated_at",
            (user_id, name, snapshot(cfg), verified_fp, now_iso()),
        )
        row = conn.execute("SELECT id FROM smtp_profiles WHERE user_id=? AND name=?", (user_id, name)).fetchone()
    return {"id": row["id"], "name": name}


def use_profile(user_id: int, profile_id: int) -> SmtpConfig:
    """Make a saved account the current SMTP settings."""
    with get_db() as conn:
        row = conn.execute("SELECT data_enc, verified_fp FROM smtp_profiles WHERE id=? AND user_id=?",
                           (profile_id, user_id)).fetchone()
    if row is None:
        raise ProfileError("Saved SMTP account not found.")
    saved = from_snapshot(row["data_enc"])
    if saved is None:  # SECRET_KEY changed since it was saved
        raise ProfileError("This saved account can no longer be read. Enter its settings again and save it.")
    save(user_id, saved, saved.password)
    cfg = load(user_id)
    if row["verified_fp"] and row["verified_fp"] == cfg.fingerprint():
        mark_verified(cfg)  # it passed the SMTP test with exactly these settings
    return cfg


def delete_profile(user_id: int, profile_id: int) -> bool:
    with get_db() as conn:
        return conn.execute("DELETE FROM smtp_profiles WHERE id=? AND user_id=?",
                            (profile_id, user_id)).rowcount > 0


def mark_profiles_verified(cfg: SmtpConfig) -> None:
    """After a successful SMTP test, remember it on saved accounts with exactly these settings."""
    fp = cfg.fingerprint()
    with get_db() as conn:
        rows = conn.execute("SELECT id, data_enc FROM smtp_profiles WHERE user_id=?", (cfg.user_id,)).fetchall()
        for r in rows:
            saved = from_snapshot(r["data_enc"])
            if saved is not None and saved.fingerprint() == fp:
                conn.execute("UPDATE smtp_profiles SET verified_fp=? WHERE id=?", (fp, r["id"]))
