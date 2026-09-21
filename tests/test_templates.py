"""Saved email templates (templates/<id>/template.json + attachment copies)."""
import json

from backend.app.config import settings

from .conftest import ADMIN_LOGIN, UID  # noqa: F401

PDF = b"%PDF-1.4 profile"
COMPOSE = {"subject": "Monthly update", "body": "<p>Hello {{name}}</p>", "format": "html",
           "personalize": True, "name_fallback": "friend"}


def test_save_list_use_update_delete(auth_client):
    c = auth_client
    att = c.post("/api/attachments", files={"file": ("profile.pdf", PDF, "application/pdf")}).json()
    saved = c.post("/api/templates", json={"name": "Monthly", "compose": COMPOSE, "attachment_ids": [att["id"]]}).json()
    tid = saved["id"]

    # stored as a folder with a readable JSON file and a copy of the attachment
    folder = settings.templates_dir / str(UID) / tid
    data = json.loads((folder / "template.json").read_text(encoding="utf-8"))
    assert data["name"] == "Monthly" and data["subject"] == "Monthly update"
    assert (folder / data["attachments"][0]["file"]).read_bytes() == PDF

    # removing the draft attachment does not affect the template
    c.delete(f"/api/attachments/{att['id']}")
    listed = c.get("/api/templates").json()
    assert listed == [{"id": tid, "name": "Monthly", "subject": "Monthly update", "format": "html",
                       "updated_at": data["updated_at"], "attachments": ["profile.pdf"]}]

    used = c.post(f"/api/templates/{tid}/use").json()
    assert used["compose"] == COMPOSE
    assert used["attachments"][0]["filename"] == "profile.pdf"
    assert [a["id"] for a in c.get("/api/attachments").json()] == [used["attachments"][0]["id"]]

    # update without attachments removes the stored copy
    c.post("/api/templates", json={"name": "Monthly v2", "compose": {**COMPOSE, "subject": "New"},
                                   "attachment_ids": [], "template_id": tid})
    data = json.loads((folder / "template.json").read_text(encoding="utf-8"))
    assert data["name"] == "Monthly v2" and data["subject"] == "New" and data["attachments"] == []
    assert list(folder.glob("att-*.bin")) == []

    assert c.delete(f"/api/templates/{tid}").json()["ok"]
    assert not folder.exists() and c.get("/api/templates").json() == []


def test_template_validation(auth_client):
    assert auth_client.post("/api/templates", json={"name": "  ", "compose": COMPOSE}).status_code == 422
    assert auth_client.post("/api/templates/../../etc/use").status_code == 404
    assert auth_client.post("/api/templates/zzzzzzzzzzzz/use").status_code == 404
    assert auth_client.delete("/api/templates/abcdef123456").status_code == 404
