"""User accounts: registration, admin controls, and isolation of each user's data."""
import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.main import app
from backend.app.sending.engine import engine
from backend.app.users import store as user_store

from .conftest import ADMIN_LOGIN, UID


def new_client():
    return TestClient(app)


def register(client, email, password="user-password-1"):
    res = client.post("/api/auth/register", json={"email": email, "password": password})
    if res.status_code == 200 and res.json().get("csrf"):
        client.headers["X-CSRF-Token"] = res.json()["csrf"]
    return res


def login(client, email, password):
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    if res.status_code == 200:
        client.headers["X-CSRF-Token"] = res.json()["csrf"]
    return res


def test_password_hashing():
    stored = user_store.hash_password("secret-123")
    assert stored.startswith("scrypt$") and "secret-123" not in stored
    assert user_store.verify_password("secret-123", stored)
    assert not user_store.verify_password("secret-124", stored)


def test_anyone_can_register_and_sign_in():
    with new_client() as c:
        res = register(c, "Alice@Example.com")
        assert res.status_code == 200 and res.json()["pending"] is False
        me = c.get("/api/auth/session").json()
        assert me["authenticated"] and me["user"]["email"] == "alice@example.com" and me["user"]["role"] == "user"
        assert c.get("/api/smtp").status_code == 200
    with new_client() as c2:
        assert login(c2, "alice@example.com", "user-password-1").status_code == 200
        assert login(c2, "alice@example.com", "wrong-password").status_code == 401


def test_registration_validation(monkeypatch):
    with new_client() as c:
        assert register(c, "not-an-email").status_code == 422
        assert register(c, "bob@example.com", "short").status_code == 422
        assert register(c, "bob@example.com").status_code == 200
    with new_client() as c:
        assert "already exists" in register(c, "BOB@example.com").json()["detail"]
    monkeypatch.setattr(settings, "allow_registration", False)
    with new_client() as c:
        assert register(c, "carol@example.com").status_code == 422


def test_approval_mode(monkeypatch):
    monkeypatch.setattr(settings, "registration_requires_approval", True)
    with new_client() as c:
        res = register(c, "dave@example.com")
        assert res.json()["pending"] is True and "csrf" not in res.json()
        assert "waiting for approval" in login(c, "dave@example.com", "user-password-1").json()["detail"]
    with new_client() as a:
        login(a, **ADMIN_LOGIN)
        user = next(u for u in a.get("/api/admin/users").json() if u["email"] == "dave@example.com")
        assert user["status"] == "pending"
        assert a.post(f"/api/admin/users/{user['id']}/status", json={"status": "active"}).status_code == 200
    with new_client() as c:
        assert login(c, "dave@example.com", "user-password-1").status_code == 200


def test_admin_endpoints_require_admin():
    with new_client() as c:
        register(c, "eve@example.com")
        assert c.get("/api/admin/users").status_code == 403
        assert c.delete(f"/api/admin/users/{UID}").status_code == 403
    with new_client() as c:
        assert c.get("/api/admin/users").status_code == 401


def test_disable_signs_user_out_and_blocks_login():
    with new_client() as u, new_client() as a:
        register(u, "frank@example.com")
        uid = u.get("/api/auth/session").json()["user"]["id"]
        login(a, **ADMIN_LOGIN)
        assert a.post(f"/api/admin/users/{uid}/status", json={"status": "disabled"}).json()["status"] == "disabled"
        assert u.get("/api/smtp").status_code == 401  # existing session is gone
        assert "disabled" in login(u, "frank@example.com", "user-password-1").json()["detail"]
        a.post(f"/api/admin/users/{uid}/status", json={"status": "active"})
        assert login(u, "frank@example.com", "user-password-1").status_code == 200


def test_main_admin_and_self_are_protected():
    with new_client() as a:
        login(a, **ADMIN_LOGIN)
        assert a.post(f"/api/admin/users/{UID}/status", json={"status": "disabled"}).status_code == 422
        assert a.delete(f"/api/admin/users/{UID}").status_code == 422


def test_users_cannot_see_each_others_data():
    with new_client() as a, new_client() as b:
        register(a, "anna@example.com")
        register(b, "ben@example.com")

        a.put("/api/smtp", json={"host": "smtp.anna.example", "from_email": "anna@example.com", "password": "anna-pw"})
        assert b.get("/api/smtp").json()["host"] == ""  # own, empty settings

        att = a.post("/api/attachments", files={"file": ("a.pdf", b"%PDF-1.4 anna", "application/pdf")}).json()
        assert b.get("/api/attachments").json() == []
        assert b.delete(f"/api/attachments/{att['id']}").status_code == 404
        res = b.post("/api/send/prepare", json={"compose": {"subject": "S", "body": "B"},
                                                "recipients_text": "x@example.org", "attachment_ids": [att["id"]]})
        assert any("attachment was not found" in e for e in res.json()["errors"])

        tpl = a.post("/api/templates", json={"name": "Anna's", "compose": {"subject": "S", "body": "B"}}).json()
        assert b.get("/api/templates").json() == []
        assert b.post(f"/api/templates/{tpl['id']}/use").status_code == 404

        a.post("/api/suppression", json={"text": "blocked@example.org"})
        assert b.get("/api/suppression").json() == []

        payload = {"compose": {"subject": "S", "body": "B"}, "recipients_text": "x@example.org",
                   "speed": "fast", "confirm": True}
        job_id = a.post("/api/send", json=payload).json()["job_id"]
        engine.wait(30)
        assert b.get(f"/api/jobs/{job_id}").status_code == 404
        assert b.get(f"/api/jobs/{job_id}/export").status_code == 404
        assert b.get("/api/jobs/latest").json() is None
        assert b.get("/api/dev/outbox").json() == []
        assert len(a.get("/api/dev/outbox").json()) == 1


def test_users_can_send_at_the_same_time(monkeypatch):
    monkeypatch.setattr(settings, "speed_delays", {"conservative": 1, "normal": 1, "fast": 1})
    payload = {"compose": {"subject": "S", "body": "B"}, "recipients_text": "x@example.org y@example.org",
               "batch_size": 1, "speed": "fast", "confirm": True}
    with new_client() as a, new_client() as b:
        register(a, "gina@example.com")
        register(b, "hank@example.com")
        a.put("/api/smtp", json={"from_email": "gina@example.com"})
        b.put("/api/smtp", json={"from_email": "hank@example.com"})
        assert a.post("/api/send", json=payload).status_code == 200
        assert b.post("/api/send", json=payload).status_code == 200  # not blocked by Gina's send
        assert a.post("/api/send", json=payload).status_code == 409  # but one at a time per user
        engine.wait(30)


def test_delete_user_removes_all_data():
    from backend.app.db import get_db

    with new_client() as u, new_client() as a:
        register(u, "ivan@example.com")
        uid = u.get("/api/auth/session").json()["user"]["id"]
        u.put("/api/smtp", json={"from_email": "ivan@example.com", "password": "pw"})
        att = u.post("/api/attachments", files={"file": ("i.pdf", b"%PDF-1.4 ivan", "application/pdf")}).json()
        u.post("/api/templates", json={"name": "T", "compose": {"subject": "S", "body": "B"}, "attachment_ids": [att["id"]]})
        u.post("/api/send", json={"compose": {"subject": "S", "body": "B"}, "recipients_text": "x@example.org",
                                  "speed": "fast", "confirm": True})
        engine.wait(30)

        login(a, **ADMIN_LOGIN)
        assert a.delete(f"/api/admin/users/{uid}").json()["ok"]
        assert u.get("/api/smtp").status_code == 401
        with get_db() as conn:
            for table in ("jobs", "attachments", "dev_outbox", "user_settings", "suppressions"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (uid,)).fetchone()[0] == 0
        assert not (settings.templates_dir / str(uid)).exists()
        assert not list(settings.upload_dir.glob(att["id"] + "*"))


def test_change_password():
    with new_client() as c:
        register(c, "judy@example.com")
        assert c.post("/api/auth/password", json={"current_password": "wrong", "new_password": "new-password-1"}).status_code == 422
        assert c.post("/api/auth/password", json={"current_password": "user-password-1",
                                                  "new_password": "new-password-1"}).status_code == 200
    with new_client() as c:
        assert login(c, "judy@example.com", "new-password-1").status_code == 200


@pytest.mark.parametrize("path", ["/api/smtp", "/api/templates", "/api/attachments", "/api/jobs/latest"])
def test_every_data_route_needs_login(path):
    with new_client() as c:
        assert c.get(path).status_code == 401


def test_regular_users_cannot_probe_private_network(smtp_server):
    controller, _ = smtp_server
    with new_client() as c:
        register(c, "kim@example.com")
        c.put("/api/smtp", json={"host": "127.0.0.1", "port": controller.port, "security": "none"})
        res = c.post("/api/smtp/test").json()
        assert not res["ok"] and "standard SMTP port" in res["message"]
        for host in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "localhost"):
            c.put("/api/smtp", json={"host": host, "port": 587, "security": "starttls"})
            res = c.post("/api/smtp/test").json()
            assert not res["ok"] and "private or local network" in res["message"], host


def test_admin_may_use_local_smtp(smtp_server):
    from backend.app.smtp import config_store
    from backend.app.smtp.client import test_connection

    from .conftest import configure_smtp

    controller, _ = smtp_server
    cfg = configure_smtp(controller.port)
    assert config_store.load(UID).trusted and test_connection(cfg)["ok"]
