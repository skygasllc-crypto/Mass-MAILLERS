"""Sending engine: BCC batching, privacy, retries and progress, against a local mock SMTP server."""
import email
from email import policy

import pytest

from backend.app.config import settings
from backend.app.db import get_db
from backend.app.recipients.parser import Recipient
from backend.app.sending.engine import SendRequest, create_job, engine, job_progress, job_results, prepare
from backend.app.sending.message import Compose
from backend.app.smtp import config_store
from backend.app.smtp.client import test_connection as run_smtp_test

from .conftest import ADMIN_LOGIN, UID, configure_smtp


def recipients(n, domain="example.org", start=0):
    return [Recipient(f"user{i}@{domain}") for i in range(start, start + n)]


def run_job(req):
    summary = prepare(req)
    assert summary["ok"], summary["errors"]
    job_id = create_job(req)
    engine.start(job_id, UID, config_store.load(UID).account_key)
    engine.wait(30)
    return job_id


@pytest.fixture
def production(smtp_server, monkeypatch):
    controller, handler = smtp_server
    monkeypatch.setattr(settings, "email_mode", "production")
    cfg = configure_smtp(controller.port)
    assert run_smtp_test(cfg)["ok"]
    config_store.mark_verified(cfg)
    return handler


def test_bcc_batches_and_privacy(production):
    handler = production
    rcpts = recipients(250)
    job_id = run_job(SendRequest(UID, Compose("Important Business Update", "<p>Hello</p>"), rcpts, 100, "fast", []))

    assert [len(m["rcpts"]) for m in handler.messages] == [100, 100, 50]
    assert sorted(sum((m["rcpts"] for m in handler.messages), [])) == sorted(r.email for r in rcpts)
    for m in handler.messages:
        raw = m["data"].decode()
        msg = email.message_from_string(raw, policy=policy.default)
        assert msg["To"] == "sender@example.com"
        assert msg["Reply-To"] == "sales@example.com"
        assert msg["From"] == "Example News <sender@example.com>"
        assert "Bcc" not in msg
        assert "user" not in raw  # no recipient address anywhere in headers or body
        assert m["from"] == "sender@example.com"

    progress = job_progress(job_id)
    assert progress["status"] == "completed"
    assert progress["successful"] == 250 and progress["failed"] == 0
    assert progress["percent"] == 100 and progress["total_batches"] == 3 and progress["current_batch"] == 3


def test_batch_size_is_capped(production, monkeypatch):
    monkeypatch.setattr(settings, "max_batch_size", 40)
    req = SendRequest(UID, Compose("S", "B"), recipients(100), 100, "fast", [])
    assert not prepare(req)["ok"]  # user asked for more than the server limit
    run_job(SendRequest(UID, Compose("S", "B"), recipients(100), 40, "fast", []))
    assert max(len(m["rcpts"]) for m in production.messages) <= 40


def test_permanent_failure_not_retried(production):
    handler = production
    rcpts = recipients(3) + [Recipient("gone@bounce.example")]
    job_id = run_job(SendRequest(UID, Compose("S", "B"), rcpts, 100, "fast", []))
    failed = job_results(job_id, "failed")
    assert len(failed) == 1
    assert failed[0]["email"] == "gone@bounce.example"
    assert failed[0]["smtp_response"].startswith("550")
    assert failed[0]["attempts"] == 1
    assert handler.rcpt_attempts["gone@bounce.example"] == 1
    assert job_progress(job_id)["successful"] == 3


def test_temporary_failure_limited_retry(production):
    handler = production
    rcpts = recipients(2) + [Recipient("busy@temp.example")]
    job_id = run_job(SendRequest(UID, Compose("S", "B"), rcpts, 100, "fast", []))
    failed = job_results(job_id, "failed")
    assert [r["email"] for r in failed] == ["busy@temp.example"]
    assert failed[0]["attempts"] == settings.max_retries + 1
    assert "gave up" in failed[0]["smtp_response"]
    assert handler.rcpt_attempts["busy@temp.example"] == settings.max_retries + 1
    # successful recipients were not sent twice
    assert handler.rcpt_attempts["user0@example.org"] == 1


def test_temporary_failure_succeeds_on_retry(production):
    handler = production
    job_id = run_job(SendRequest(UID, Compose("S", "B"), [Recipient("grey@flaky.example")], 100, "fast", []))
    rows = job_results(job_id)
    assert rows[0]["status"] == "sent" and rows[0]["attempts"] == 2
    assert len(handler.messages) == 1


def test_personalization_sends_individual_messages(production):
    handler = production
    rcpts = [Recipient("john@example.org", "John"), Recipient("mary@example.org", "Mary"), Recipient("x@example.org")]
    compose = Compose("Hello {{name}}", "<p>Hello {{name}},</p>", personalize=True, name_fallback="friend")
    run_job(SendRequest(UID, compose, rcpts, 2, "fast", []))
    assert len(handler.messages) == 3
    by_rcpt = {m["rcpts"][0]: email.message_from_bytes(m["data"], policy=policy.default) for m in handler.messages}
    assert all(len(m["rcpts"]) == 1 for m in handler.messages)
    assert by_rcpt["john@example.org"]["Subject"] == "Hello John"
    assert by_rcpt["john@example.org"]["To"] == "john@example.org"
    assert "Hello Mary," in by_rcpt["mary@example.org"].get_body(("html",)).get_content()
    assert by_rcpt["x@example.org"]["Subject"] == "Hello friend"


def test_progress_is_recorded_per_batch(production, monkeypatch):
    seen = []
    original = engine._update_job

    def spy(job_id, **fields):
        original(job_id, **fields)
        if "current_batch" in fields:
            seen.append(job_progress(job_id)["completed"])

    monkeypatch.setattr(engine, "_update_job", spy)
    run_job(SendRequest(UID, Compose("S", "B"), recipients(30), 10, "fast", []))
    assert seen == [0, 10, 20]


def test_attachments_are_sent(production):
    from backend.app.attachments import store

    att = store.save(UID, "company-profile.pdf", b"%PDF-1.4 hello")
    run_job(SendRequest(UID, Compose("S", "<p>B</p>"), recipients(2), 100, "fast", [att.id]))
    msg = email.message_from_bytes(production.messages[0]["data"], policy=policy.default)
    assert [a.get_filename() for a in msg.iter_attachments()] == ["company-profile.pdf"]


def test_production_requires_verified_connection(smtp_server, monkeypatch):
    controller, handler = smtp_server
    monkeypatch.setattr(settings, "email_mode", "production")
    configure_smtp(controller.port)
    summary = prepare(SendRequest(UID, Compose("S", "B"), recipients(1), 100, "fast", []))
    assert not summary["ok"]
    assert any("Test SMTP Connection" in e for e in summary["errors"])


def test_changed_settings_require_new_test(production):
    cfg = config_store.load(UID)
    config_store.save(UID, config_store.SmtpConfig(**{**cfg.__dict__, "port": cfg.port + 1}), None)
    assert not config_store.is_verified(config_store.load(UID))


def test_auth_failure_stops_job(production):
    cfg = config_store.load(UID)
    config_store.save(UID, cfg, "wrong-password")
    config_store.mark_verified(config_store.load(UID))
    job_id = run_job(SendRequest(UID, Compose("S", "B"), recipients(5), 2, "fast", []))
    progress = job_progress(job_id)
    assert progress["status"] == "failed"
    assert "authentication" in progress["status_text"].lower()
    assert progress["not_sent"] == 5
    assert production.messages == []


def test_development_mode_never_sends(smtp_server):
    controller, handler = smtp_server
    configure_smtp(controller.port)
    rcpts = recipients(5) + [Recipient("x@bounce.test"), Recipient("y@retry.test")]
    job_id = run_job(SendRequest(UID, Compose("S", "B"), rcpts, 3, "fast", []))
    assert handler.messages == []  # nothing reached the SMTP server
    with get_db() as conn:
        rows = conn.execute("SELECT envelope_to FROM dev_outbox").fetchall()
    assert len(rows) == 3
    progress = job_progress(job_id)
    assert progress["successful"] == 6 and progress["failed"] == 1


def test_conservative_delay_choices(monkeypatch):
    from backend.app.config import settings
    from backend.app.sending.engine import batch_delay

    monkeypatch.setattr(settings, "speed_delays", {"conservative": 30, "normal": 10, "fast": 3})
    for seconds in (10, 30, 60, 120, 300, 600, 1200):
        assert batch_delay("conservative", seconds) == seconds
    assert batch_delay("conservative", None) == 30   # default
    assert batch_delay("conservative", 7) == 30      # not an allowed choice
    assert batch_delay("normal", 600) == 10          # only applies to conservative
