"""Application configuration, loaded from environment variables / .env."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]  # mass-mailer/
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("mailer")

# Seconds to wait between BCC batches for each sending mode.
SPEED_DELAYS = {"conservative": 30.0, "normal": 10.0, "fast": 3.0}
# Pauses (seconds) the user can pick for conservative mode: 10s, 30s, 1m, 2m, 5m, 10m, 20m.
CONSERVATIVE_DELAY_CHOICES = (10, 30, 60, 120, 300, 600, 1200)
# Seconds between individual messages ("individual" delivery / personalization). Microsoft 365, for
# example, allows about 30 messages per minute, so conservative stays at one message every 2 s.
MESSAGE_DELAYS = {"conservative": 2.0, "normal": 1.0, "fast": 0.5}

DEFAULT_ALLOWED_EXTENSIONS = "pdf,doc,docx,xls,xlsx,csv,png,jpg,jpeg,zip,txt"
EXECUTABLE_EXTENSIONS = {
    "exe", "bat", "cmd", "scr", "ps1", "com", "msi", "vbs", "vbe", "js", "jse",
    "wsf", "wsh", "hta", "cpl", "jar", "lnk", "reg", "pif", "dll", "sh", "apk",
}


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


def _read_or_create(path: Path, generate) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = generate()
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


@dataclass
class Settings:
    email_mode: str = "development"
    admin_password: str = ""
    admin_email: str = "admin"
    allow_registration: bool = True
    registration_requires_approval: bool = False
    secret_key: str = ""
    data_dir: Path = BASE_DIR / "data"
    upload_dir: Path = BASE_DIR / "uploads"
    templates_dir: Path = BASE_DIR / "templates"
    frontend_dir: Path = BASE_DIR / "frontend"
    host: str = "127.0.0.1"
    port: int = 8000
    cookie_secure: bool = False
    trusted_proxy_hops: int = 0  # reverse proxies in front of the app (Render: 1)
    session_hours: int = 12

    max_recipients: int = 10000
    max_parallel_sends: int = 50  # per user, each through a different SMTP account
    default_batch_size: int = 100
    max_batch_size: int = 500  # hard cap: never put more than this many RCPTs in one message
    max_attachment_mb: float = 10
    max_message_mb: float = 20
    allowed_extensions: set[str] = field(default_factory=set)
    allow_executable_attachments: bool = False
    allow_private_smtp_hosts: bool = False

    smtp_timeout: int = 30
    smtp_tls_verify: bool = True
    max_retries: int = 2
    retry_delay: float = 60.0
    speed_delays: dict[str, float] = field(default_factory=dict)
    message_delays: dict[str, float] = field(default_factory=dict)

    @property
    def is_development(self) -> bool:
        return self.email_mode != "production"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mailer.db"


def load_settings() -> Settings:
    mode = os.getenv("EMAIL_MODE", "development").strip().lower()
    if mode not in {"development", "production"}:
        raise RuntimeError("EMAIL_MODE must be 'development' or 'production'")
    dev = mode == "development"

    data_dir = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
    upload_dir = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads")))
    data_dir.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)
    templates_dir = Path(os.getenv("TEMPLATES_DIR", str(BASE_DIR / "templates")))
    templates_dir.mkdir(parents=True, exist_ok=True)

    secret_key = os.getenv("SECRET_KEY", "").strip()
    if not secret_key:
        if not dev:
            raise RuntimeError("SECRET_KEY must be set in production mode (see .env.example)")
        secret_key = _read_or_create(data_dir / ".secret_key", lambda: secrets.token_urlsafe(48))

    admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not admin_password:
        if not dev:
            raise RuntimeError("ADMIN_PASSWORD must be set in production mode (see .env.example)")
        pw_file = data_dir / "admin_password.txt"
        admin_password = _read_or_create(pw_file, lambda: secrets.token_urlsafe(12))
        log.warning("ADMIN_PASSWORD not set - using generated password stored in %s", pw_file)
    elif not dev and len(admin_password) < 10:
        raise RuntimeError("ADMIN_PASSWORD must be at least 10 characters in production mode")

    # Development mode shortens delays so the local test mailbox fills quickly.
    scale = 0.1 if dev else 1.0
    delays = {k: v * scale for k, v in SPEED_DELAYS.items()}
    message_delays = {k: v * scale for k, v in MESSAGE_DELAYS.items()}

    allowed = {
        e.strip().lower().lstrip(".")
        for e in os.getenv("ALLOWED_ATTACHMENT_EXTENSIONS", DEFAULT_ALLOWED_EXTENSIONS).split(",")
        if e.strip()
    }

    return Settings(
        email_mode=mode,
        admin_password=admin_password,
        admin_email=os.getenv("ADMIN_EMAIL", "admin").strip().lower() or "admin",
        allow_registration=_bool("ALLOW_REGISTRATION", True),
        registration_requires_approval=_bool("REGISTRATION_REQUIRES_APPROVAL", False),
        secret_key=secret_key,
        data_dir=data_dir,
        upload_dir=upload_dir,
        templates_dir=templates_dir,
        host=os.getenv("HOST", "127.0.0.1"),
        port=_int("PORT", 8000),
        cookie_secure=_bool("COOKIE_SECURE", not dev),
        trusted_proxy_hops=_int("TRUSTED_PROXY_HOPS", 0),
        session_hours=_int("SESSION_HOURS", 12),
        max_recipients=_int("MAX_RECIPIENTS", 10000),
        max_parallel_sends=_int("MAX_PARALLEL_SENDS", 50),
        default_batch_size=_int("DEFAULT_BATCH_SIZE", 100),
        max_batch_size=_int("SMTP_MAX_RECIPIENTS_PER_MESSAGE", 500),
        max_attachment_mb=_float("MAX_ATTACHMENT_MB", 10),
        max_message_mb=_float("MAX_MESSAGE_MB", 20),
        allowed_extensions=allowed,
        allow_executable_attachments=_bool("ALLOW_EXECUTABLE_ATTACHMENTS", False),
        allow_private_smtp_hosts=_bool("ALLOW_PRIVATE_SMTP_HOSTS", False),
        smtp_timeout=_int("SMTP_TIMEOUT", 30),
        smtp_tls_verify=_bool("SMTP_TLS_VERIFY", True),
        max_retries=_int("MAX_RETRIES", 2),
        retry_delay=_float("RETRY_DELAY_SECONDS", 1.0 if dev else 60.0),
        speed_delays=delays,
        message_delays=message_delays,
    )


settings = load_settings()
