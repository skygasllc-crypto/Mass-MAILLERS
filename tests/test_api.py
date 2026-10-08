"""HTTP API: authentication, CSRF, rate limiting and the complete send workflow (development mode)."""
import csv
import io
import time

import pytest

from backend.app.config import settings
from backend.app.utils.security import SESSION_COOKIE

from .conftest import ADMIN_LOGIN, UID  # noqa: F401


def wait_for_job(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_requires_login(client):
    assert client.get("/api/smtp").status_code == 401
    assert client.post("/api/recipients/parse", json={"text": ""}).status_code == 401
    assert client.get("/api/auth/session").json()["authenticated"] is False


def test_wrong_password_and_rate_limit(client):
    for _ in range(10):
        assert client.post("/api/auth/login", json={"email": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "admin", "password": "nope"}).status_code == 429


def test_session_cookie_flags(client):
    res = client.post("/api/auth/login", json=ADMIN_LOGIN)
    cookie = res.headers["set-cookie"]
    assert SESSION_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie


def test_csrf_required_for_writes(auth_client):
    token = auth_client.headers.pop("X-CSRF-Token")
    assert auth_client.put("/api/smtp", json={"host": "x"}).status_code == 403
    auth_client.headers["X-CSRF-Token"] = token
    assert auth_client.put("/api/smtp", json={"host": "smtp.example.com"}).status_code == 200


def test_password_never_returned(auth_client):
    res = auth_client.put("/api/smtp", json={"host": "smtp.example.com", "username": "u", "password": "s3cret!"})
    assert res.status_code == 200
    assert "s3cret!" not in res.text and res.json()["password_set"] is True
    # blank password keeps the stored one
    res = auth_client.put("/api/smtp", json={"host": "smtp.example.com", "username": "u", "password": ""})
    assert res.json()["password_set"] is True
    assert "s3cret!" not in auth_client.get("/api/smtp").text


def test_security_headers(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "default-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-frame-options"] == "DENY"


def test_send_requires_confirmation(auth_client):
    body = {"compose": {"subject": "S", "body": "B"}, "recipients_text": "a@example.com"}
    assert auth_client.post("/api/send", json=body).status_code == 400


def test_invalid_reply_to_blocks_sending(auth_client):
    auth_client.put("/api/smtp", json={"from_email": "news@example.com", "reply_to": "not-an-email"})
    res = auth_client.post("/api/send/prepare", json={"compose": {"subject": "S", "body": "B"},
                                                       "recipients_text": "a@example.com"})
    assert res.json()["ok"] is False
    assert "Reply-To" in " ".join(res.json()["errors"])


def test_oversized_message_blocked(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "max_message_mb", 0.01)
    auth_client.put("/api/smtp", json={"from_email": "news@example.com"})
    att = auth_client.post("/api/attachments", files={"file": ("big.pdf", b"%PDF" + b"x" * 20000, "application/pdf")}).json()
    res = auth_client.post("/api/send/prepare", json={"compose": {"subject": "S", "body": "B"},
                                                       "recipients_text": "a@example.com", "attachment_ids": [att["id"]]})
    assert not res.json()["ok"] and "too large" in res.json()["errors"][0]


def test_recipient_limit(auth_client):
    text = "\n".join(f"u{i}@example.com" for i in range(settings.max_recipients + 1))
    assert auth_client.post("/api/recipients/parse", json={"text": text}).json()["over_limit"] is True


def test_full_workflow_1000_recipients(auth_client):
    """The acceptance workflow from the spec, end to end, in development mode."""
    c = auth_client
    # Configure sender
    res = c.put("/api/smtp", json={"host": "smtp.example.com", "port": 587, "security": "starttls",
                                   "from_name": "Example Co", "from_email": "newsletter@example.com",
                                   "reply_to": "sales@example.com"})
    assert res.status_code == 200

    # Import a CSV with 1,000 rows incl. duplicates, invalid and simulated failures
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "email"])
    for i in range(985):
        w.writerow([f"Person {i}", f"person{i}@example.org"])
    for i in range(5):
        w.writerow([f"Dup {i}", f"PERSON{i}@example.org"])  # duplicates
    for i in range(5):
        w.writerow([f"Bad {i}", f"bad{i}@"])  # invalid
    for i in range(3):
        w.writerow([f"Bounce {i}", f"b{i}@bounce.test"])  # simulated 550
    for i in range(2):
        w.writerow([f"Retry {i}", f"r{i}@retry.test"])  # simulated 451 then OK
    res = c.post("/api/recipients/import", files={"file": ("list.csv", buf.getvalue().encode(), "text/csv")})
    data = res.json()
    assert data["stats"] == {"total": 1000, "valid": 990, "invalid": 5, "duplicates": 5, "ready": 990, "named": 990,
                             "suppressed": 0}

    # Remove invalid -> clean text
    clean = c.post("/api/recipients/parse", json={"text": data["clean_text"]}).json()
    assert clean["stats"]["valid"] == 990 and clean["stats"]["invalid"] == 0

    # Compose HTML + attachment
    att = c.post("/api/attachments", files={"file": ("company-profile.pdf", b"%PDF-1.4 profile", "application/pdf")}).json()
    compose = {"subject": "Important Business Update", "format": "html",
               "body": "<p>Hello,</p><p>We would like to introduce our company and services.</p><p>Best regards,<br>Company Team</p>"}

    # Preview
    preview = c.post("/api/preview", json={"compose": compose, "attachment_ids": [att["id"]]}).json()
    assert "introduce our company" in preview["html"] and "introduce our company" in preview["text"]

    # Send test
    res = c.post("/api/send/test", json={"compose": compose, "attachment_ids": [att["id"]], "test_email": "test@example.com"})
    assert res.json()["ok"], res.json()
    outbox = c.get("/api/dev/outbox").json()
    assert len(outbox) == 1 and outbox[0]["envelope_to"] == ["test@example.com"]
    test_msg = c.get(f"/api/dev/outbox/{outbox[0]['id']}").json()
    headers = dict(test_msg["headers"])
    assert headers["Reply-To"] == "sales@example.com"
    assert headers["Subject"] == "Important Business Update"
    assert test_msg["attachments"][0]["filename"] == "company-profile.pdf"
    assert "multipart/mixed" in test_msg["structure"] and "multipart/alternative" in test_msg["structure"]

    # Confirmation summary
    payload = {"compose": compose, "recipients_text": data["clean_text"], "batch_size": 100,
               "speed": "conservative", "attachment_ids": [att["id"]]}
    summary = c.post("/api/send/prepare", json=payload).json()
    assert summary["ok"] and summary["recipients"] == 990 and summary["batches"] == 10
    assert summary["reply_to"] == "sales@example.com" and summary["attachments"] == ["company-profile.pdf"]

    # Send (background) and poll progress
    job_id = c.post("/api/send", json={**payload, "confirm": True}).json()["job_id"]
    job = wait_for_job(c, job_id)
    assert job["status"] == "completed"
    assert job["total"] == 990 and job["successful"] == 987 and job["failed"] == 3
    assert job["total_batches"] == 10 and job["percent"] == 100

    # Mailbox: 1 test + 10 BCC batches + 1 retry message for the two *@retry.test addresses
    outbox = c.get("/api/dev/outbox").json()
    assert len(outbox) == 12
    assert sorted(outbox[0]["envelope_to"]) == ["r0@retry.test", "r1@retry.test"]
    assert max(len(m["envelope_to"]) for m in outbox) <= 100
    campaign = c.get(f"/api/dev/outbox/{outbox[1]['id']}").json()
    header_text = "\n".join(f"{k}: {v}" for k, v in campaign["headers"])
    assert "person" not in header_text and "Bcc" not in header_text

    # View + export failed
    failed = c.get(f"/api/jobs/{job_id}/results?status=failed").json()
    assert sorted(r["email"] for r in failed) == ["b0@bounce.test", "b1@bounce.test", "b2@bounce.test"]
    assert all(r["smtp_response"].startswith("550") for r in failed)
    export = c.get(f"/api/jobs/{job_id}/export?status=failed")
    assert export.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(export.text)))
    assert rows[0] == ["email", "name", "status", "smtp_response", "attempts", "batch", "timestamp"]
    assert len(rows) == 4 and rows[1][2] == "FAILED"

    full = list(csv.reader(io.StringIO(c.get(f"/api/jobs/{job_id}/export").text)))
    assert len(full) == 991
    assert c.get("/api/jobs/latest").json()["id"] == job_id


def test_one_send_per_smtp_account(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "speed_delays", {"conservative": 5, "normal": 5, "fast": 5})
    auth_client.put("/api/smtp", json={"from_email": "news@example.com"})
    payload = {"compose": {"subject": "S", "body": "B"}, "recipients_text": "a@example.com b@example.com",
               "batch_size": 1, "speed": "fast", "confirm": True}
    job_id = auth_client.post("/api/send", json=payload).json()["job_id"]
    res = auth_client.post("/api/send", json=payload)
    assert res.status_code == 409 and "already sending" in res.json()["detail"]
    deadline = time.time() + 20  # wait until the first batch is out, then stop the rest
    while auth_client.get(f"/api/jobs/{job_id}").json()["successful"] < 1 and time.time() < deadline:
        time.sleep(0.1)
    assert auth_client.post(f"/api/jobs/{job_id}/cancel").json()["ok"] is True
    job = wait_for_job(auth_client, job_id)
    assert job["status"] == "cancelled"
    assert job["successful"] == 1 and job["not_sent"] == 1


def test_sends_continue_after_logout_and_use_their_own_smtp_account(auth_client, monkeypatch):
    from backend.app.db import get_db

    monkeypatch.setattr(settings, "speed_delays", {"conservative": 2, "normal": 2, "fast": 2})
    payload = {"compose": {"subject": "S", "body": "B"}, "recipients_text": "a@example.com b@example.com",
               "batch_size": 1, "speed": "fast", "confirm": True}

    auth_client.put("/api/smtp", json={"username": "first@example.com", "password": "pw1",
                                       "from_email": "first@example.com"})
    first = auth_client.post("/api/send", json=payload).json()["job_id"]
    # Same user, different SMTP account: a second send runs alongside the first.
    auth_client.put("/api/smtp", json={"username": "second@example.com", "password": "pw2",
                                       "from_email": "second@example.com"})
    second = auth_client.post("/api/send", json=payload).json()["job_id"]

    auth_client.post("/api/auth/logout")
    assert auth_client.get("/api/jobs/active").status_code == 401
    res = auth_client.post("/api/auth/login", json=ADMIN_LOGIN)
    auth_client.headers["X-CSRF-Token"] = res.json()["csrf"]
    active = {j["id"]: j for j in auth_client.get("/api/jobs/active").json()}
    assert set(active) == {first, second}  # still sending after logout

    jobs = {j: wait_for_job(auth_client, j) for j in (first, second)}
    assert all(j["status"] == "completed" and j["successful"] == 2 for j in jobs.values())
    assert jobs[first]["smtp_account"].startswith("first@example.com")
    assert jobs[second]["smtp_account"].startswith("second@example.com")
    assert auth_client.get("/api/jobs/active").json() == []
    senders = [m["envelope_from"] for m in auth_client.get("/api/dev/outbox").json()]
    assert sorted(senders) == ["first@example.com"] * 2 + ["second@example.com"] * 2
    with get_db() as conn:  # the copy of the SMTP credentials is dropped when a send ends
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE smtp_enc IS NOT NULL").fetchone()[0] == 0


def test_parallel_send_limit(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "speed_delays", {"conservative": 5, "normal": 5, "fast": 5})
    monkeypatch.setattr(settings, "max_parallel_sends", 1)
    payload = {"compose": {"subject": "S", "body": "B"}, "recipients_text": "a@example.com b@example.com",
               "batch_size": 1, "speed": "fast", "confirm": True}
    auth_client.put("/api/smtp", json={"from_email": "one@example.com"})
    job_id = auth_client.post("/api/send", json=payload).json()["job_id"]
    auth_client.put("/api/smtp", json={"from_email": "two@example.com"})
    res = auth_client.post("/api/send", json=payload)
    assert res.status_code == 409 and "at most 1" in res.json()["detail"]
    auth_client.post(f"/api/jobs/{job_id}/cancel")
    wait_for_job(auth_client, job_id)


def test_client_ip_behind_proxy(client, monkeypatch):
    from starlette.requests import Request

    from backend.app.utils.security import client_ip

    def req(xff):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": ("10.0.0.1", 1234)})

    assert client_ip(req("1.2.3.4")) == "10.0.0.1"  # no proxy configured: header ignored
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    assert client_ip(req("6.6.6.6, 1.2.3.4")) == "1.2.3.4"  # spoofed left entry is ignored
    assert client_ip(req("")) == "10.0.0.1"


def test_login_limited_per_account(client, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)  # each attempt appears to come from a new IP
    for i in range(10):
        client.headers["X-Forwarded-For"] = f"9.9.9.{i}"
        client.post("/api/auth/login", json={"email": "admin", "password": "wrong"})
    assert client.post("/api/auth/login", json=ADMIN_LOGIN).status_code == 429


def test_ten_thousand_recipients_accepted(auth_client):
    text = "\n".join(f"u{i}@example.com" for i in range(10_000))
    data = auth_client.post("/api/recipients/parse", json={"text": text}).json()
    assert data["stats"]["ready"] == 10_000
    assert data["over_limit"] is False


def test_conservative_delay_in_summary(auth_client):
    auth_client.put("/api/smtp", json={"host": "smtp.example.com", "port": 587, "security": "starttls",
                                       "username": "u", "password": "p", "from_email": "news@example.com"})
    body = {"compose": {"subject": "Hi", "body": "Hello"}, "recipients_text": "a@example.com",
            "speed": "conservative", "conservative_delay": 300}
    s = auth_client.post("/api/send/prepare", json=body).json()
    assert s["delay_seconds"] == 300
    body["conservative_delay"] = 45
    s = auth_client.post("/api/send/prepare", json=body).json()
    assert not s["ok"] and any("conservative pause" in e for e in s["errors"])


def test_saved_smtp_accounts(auth_client):
    from backend.app.db import get_db
    from backend.app.smtp import config_store

    from .test_users import new_client, register

    first = {"host": "smtp.first.example", "port": 587, "security": "starttls", "username": "one@first.example",
             "password": "first-secret", "from_email": "one@first.example", "reply_to": "sales@first.example"}
    second = {"host": "smtp.second.example", "port": 465, "security": "ssl", "username": "two@second.example",
              "password": "second-secret", "from_email": "two@second.example"}
    auth_client.put("/api/smtp", json=first)
    a = auth_client.post("/api/smtp/profiles", json={"name": "First"}).json()
    auth_client.put("/api/smtp", json=second)
    b = auth_client.post("/api/smtp/profiles", json={"name": "Second"}).json()

    profiles = auth_client.get("/api/smtp/profiles").json()
    assert [p["name"] for p in profiles] == ["First", "Second"]
    assert "secret" not in str(profiles)  # passwords are never sent to the browser
    with get_db() as conn:
        assert "secret" not in str([tuple(r) for r in conn.execute("SELECT * FROM smtp_profiles")])

    current = auth_client.post(f"/api/smtp/profiles/{a['id']}/use").json()
    assert (current["host"], current["port"], current["username"], current["reply_to"]) == \
        ("smtp.first.example", 587, "one@first.example", "sales@first.example")
    assert current["password_set"] and "password" not in current
    assert config_store.load(UID).password == "first-secret"
    auth_client.post(f"/api/smtp/profiles/{b['id']}/use")
    assert config_store.load(UID).password == "second-secret"

    # Saving under an existing name replaces it instead of adding a duplicate.
    auth_client.put("/api/smtp", json={**second, "password": "rotated"})
    assert auth_client.post("/api/smtp/profiles", json={"name": "Second"}).json()["id"] == b["id"]
    auth_client.put("/api/smtp", json=first)
    auth_client.post(f"/api/smtp/profiles/{b['id']}/use")
    assert config_store.load(UID).password == "rotated"

    with new_client() as other:  # other users cannot see or use them
        register(other, "olga@example.com")
        assert other.get("/api/smtp/profiles").json() == []
        assert other.post(f"/api/smtp/profiles/{a['id']}/use").status_code == 404
        assert other.delete(f"/api/smtp/profiles/{a['id']}").status_code == 404

    assert auth_client.delete(f"/api/smtp/profiles/{a['id']}").json()["ok"]
    assert [p["name"] for p in auth_client.get("/api/smtp/profiles").json()] == ["Second"]


def test_saved_smtp_account_keeps_verification(auth_client, smtp_server):
    controller, _ = smtp_server
    from .conftest import SMTP_PASS, SMTP_USER

    good = {"host": "127.0.0.1", "port": controller.port, "security": "none", "username": SMTP_USER,
            "password": SMTP_PASS, "from_email": SMTP_USER}
    auth_client.put("/api/smtp", json=good)
    saved = auth_client.post("/api/smtp/profiles", json={"name": "Local"}).json()
    assert auth_client.post("/api/smtp/test").json()["ok"]  # test after saving still marks the saved account
    auth_client.put("/api/smtp", json={**good, "host": "smtp.other.example"})
    assert auth_client.get("/api/smtp").json()["verified"] is False
    assert auth_client.post(f"/api/smtp/profiles/{saved['id']}/use").json()["verified"] is True
