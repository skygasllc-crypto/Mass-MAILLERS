"""Test fixtures. Everything runs against a local mock SMTP server – never an external provider."""
from __future__ import annotations

import datetime
import os
import socket
import ssl
import sys
import tempfile
from collections import Counter
from pathlib import Path

import pytest

# Isolated data directories and settings BEFORE the app is imported.
_TMP = Path(tempfile.mkdtemp(prefix="mailer-tests-"))
os.environ.update({
    "EMAIL_MODE": "development",
    "DATA_DIR": str(_TMP / "data"),
    "UPLOAD_DIR": str(_TMP / "uploads"),
    "TEMPLATES_DIR": str(_TMP / "templates"),
    "ADMIN_PASSWORD": "test-password-123",
    "SECRET_KEY": "test-secret-key-for-unit-tests-only",
    "RETRY_DELAY_SECONDS": "0",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiosmtpd.controller import Controller  # noqa: E402
from aiosmtpd.smtp import AuthResult, LoginPassword  # noqa: E402

from backend.app.config import settings  # noqa: E402
from backend.app.db import get_db, init_db  # noqa: E402
from backend.app.sending.engine import engine  # noqa: E402
from backend.app.smtp import config_store  # noqa: E402
from backend.app.users.store import ensure_admin  # noqa: E402
from backend.app.utils.security import limiter  # noqa: E402

init_db()
UID = ensure_admin()["id"]  # the main admin; most tests act as this user
ADMIN_LOGIN = {"email": "admin", "password": "test-password-123"}

SMTP_USER = "sender@example.com"
SMTP_PASS = "smtp-secret"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockHandler:
    """Accepts mail; special domains simulate failures.

    bounce.example -> 550 permanent, temp.example -> 451 always, flaky.example -> 451 once then OK.
    """

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.rcpt_attempts: Counter = Counter()

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        domain = address.rsplit("@", 1)[-1].lower()
        self.rcpt_attempts[address] += 1
        if domain == "bounce.example":
            return "550 5.1.1 Mailbox unavailable"
        if domain == "temp.example":
            return "451 4.3.0 Temporary failure, try again later"
        if domain == "flaky.example" and self.rcpt_attempts[address] == 1:
            return "451 4.3.0 Greylisted, try again"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append({
            "from": envelope.mail_from,
            "rcpts": list(envelope.rcpt_tos),
            "data": envelope.original_content or envelope.content,
        })
        return "250 Message accepted for delivery"


def authenticator(server, session, envelope, mechanism, auth_data):
    ok = isinstance(auth_data, LoginPassword) and auth_data.login == SMTP_USER.encode() \
        and auth_data.password == SMTP_PASS.encode()
    return AuthResult(success=ok, handled=False)


def _self_signed(tmp: Path) -> ssl.SSLContext:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


@pytest.fixture
def smtp_server():
    """Plain SMTP with AUTH on 127.0.0.1 (auth over plaintext is allowed only for localhost)."""
    handler = MockHandler()
    controller = Controller(handler, hostname="127.0.0.1", port=free_port(),
                            authenticator=authenticator, auth_require_tls=False)
    controller.start()
    yield controller, handler
    controller.stop()


@pytest.fixture
def starttls_server(tmp_path):
    handler = MockHandler()
    controller = Controller(handler, hostname="127.0.0.1", port=free_port(), tls_context=_self_signed(tmp_path),
                            authenticator=authenticator, auth_require_tls=True)
    controller.start()
    yield controller, handler
    controller.stop()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Fresh database, no delays, development mode by default."""
    init_db()
    with get_db() as conn:
        for table in ("settings", "attachments", "jobs", "job_recipients", "dev_outbox", "suppression",
                      "user_settings", "suppressions"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM users WHERE id != ?", (UID,))
    monkeypatch.setattr(settings, "allow_registration", True)
    monkeypatch.setattr(settings, "registration_requires_approval", False)
    monkeypatch.setattr(settings, "email_mode", "development")
    monkeypatch.setattr(settings, "speed_delays", {"conservative": 0, "normal": 0, "fast": 0})
    monkeypatch.setattr(settings, "message_delays", {"conservative": 0, "normal": 0, "fast": 0})
    monkeypatch.setattr(settings, "retry_delay", 0)
    monkeypatch.setattr(settings, "smtp_tls_verify", True)
    limiter.reset()
    yield
    engine.wait(10)


def configure_smtp(port: int, security: str = "none", password: str = SMTP_PASS, **extra) -> config_store.SmtpConfig:
    cfg = config_store.SmtpConfig(
        host="127.0.0.1", port=port, security=security, username=SMTP_USER,
        from_name="Example News", from_email="sender@example.com", reply_to="sales@example.com",
        **extra,
    )
    config_store.save(UID, cfg, password)
    return config_store.load(UID)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from backend.app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client):
    res = client.post("/api/auth/login", json=ADMIN_LOGIN)
    assert res.status_code == 200
    client.headers["X-CSRF-Token"] = res.json()["csrf"]
    return client
