"""SMTP connection test against a local mock server."""
from backend.app.config import settings
from backend.app.smtp import config_store
from backend.app.smtp.client import test_connection as run_smtp_test

from .conftest import ADMIN_LOGIN, SMTP_PASS, UID, configure_smtp, free_port


def names(result):
    return {s["name"]: s["ok"] for s in result["steps"]}


def test_connection_success(smtp_server):
    controller, _ = smtp_server
    result = run_smtp_test(configure_smtp(controller.port))
    assert result["ok"], result
    assert result["message"] == "Connection successful"
    steps = names(result)
    assert steps["DNS resolution"] and steps["TCP connection"] and steps["Authentication"]
    assert steps["TLS negotiation"] is None  # plaintext localhost server: skipped, not failed


def test_authentication_failure(smtp_server):
    controller, _ = smtp_server
    result = run_smtp_test(configure_smtp(controller.port, password="wrong"))
    assert not result["ok"]
    assert result["message"] == "Authentication failed"
    assert "wrong" not in str(result) and SMTP_PASS not in str(result)  # never leak secrets


def test_dns_failure():
    cfg = config_store.SmtpConfig(host="no-such-host.invalid", port=587)
    result = run_smtp_test(cfg)
    assert not result["ok"] and names(result)["DNS resolution"] is False


def test_tcp_failure():
    cfg = config_store.SmtpConfig(host="127.0.0.1", port=free_port(), security="none", trusted=True)
    result = run_smtp_test(cfg)
    assert not result["ok"] and names(result)["TCP connection"] is False


def test_starttls_negotiation(starttls_server, monkeypatch):
    controller, _ = starttls_server
    monkeypatch.setattr(settings, "smtp_tls_verify", False)  # self-signed test certificate
    result = run_smtp_test(configure_smtp(controller.port, security="starttls"))
    assert result["ok"], result
    assert names(result)["TLS negotiation"] is True


def test_starttls_rejects_untrusted_certificate(starttls_server):
    controller, _ = starttls_server
    result = run_smtp_test(configure_smtp(controller.port, security="starttls"))
    assert not result["ok"]
    assert result["message"] == "TLS certificate could not be verified"


def test_starttls_missing_is_reported(smtp_server):
    controller, _ = smtp_server
    result = run_smtp_test(configure_smtp(controller.port, security="starttls"))
    assert not result["ok"] and "STARTTLS" in result["message"]


def test_password_is_encrypted_at_rest(smtp_server):
    from backend.app.db import get_setting

    controller, _ = smtp_server
    configure_smtp(controller.port)
    stored = get_setting(UID, "smtp_password_enc")
    assert stored and SMTP_PASS not in stored
    assert config_store.load(UID).password == SMTP_PASS
    assert "password" not in config_store.load(UID).public()


def test_production_requires_credentials(monkeypatch):
    monkeypatch.setattr(settings, "email_mode", "production")
    cfg = config_store.SmtpConfig(host="127.0.0.1", port=25, security="none")
    result = run_smtp_test(cfg)
    assert not result["ok"] and "username and password" in result["message"]
