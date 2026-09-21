"""Saved email templates, stored as folders on disk:

    templates/<user id>/<id>/template.json     name, subject, body, format, personalization
    templates/<user id>/<id>/att-*.bin         copies of the template's attachments

The folder can be backed up or copied to another installation as-is.
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

from ..attachments import store as attachment_store
from ..config import settings
from ..db import now_iso

_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_NAME_BAD = re.compile(r"[\x00-\x1f\x7f]")
COMPOSE_FIELDS = ("subject", "body", "format", "personalize", "name_fallback")


class TemplateError(ValueError):
    pass


def _root(user_id: int) -> Path:
    return settings.templates_dir / str(int(user_id))


def _folder(user_id: int, template_id: str) -> Path:
    if not _ID_RE.match(template_id or ""):
        raise TemplateError("Template not found.")
    return _root(user_id) / template_id


def _read(folder: Path) -> dict | None:
    try:
        return json.loads((folder / "template.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_templates(user_id: int) -> list[dict]:
    items = []
    if _root(user_id).exists():
        for folder in _root(user_id).iterdir():
            data = _read(folder) if folder.is_dir() and _ID_RE.match(folder.name) else None
            if data:
                items.append({
                    "id": data["id"], "name": data["name"], "subject": data.get("subject", ""),
                    "format": data.get("format", "html"), "updated_at": data.get("updated_at"),
                    "attachments": [a["filename"] for a in data.get("attachments", [])],
                })
    return sorted(items, key=lambda t: t["name"].lower())


def get(user_id: int, template_id: str) -> dict:
    data = _read(_folder(user_id, template_id))
    if data is None:
        raise TemplateError("Template not found.")
    return data


def save(user_id: int, name: str, compose: dict, attachment_ids: list[str], template_id: str | None = None) -> dict:
    """Create a template, or overwrite ``template_id``. Attachments are copied into the folder."""
    name = " ".join(_NAME_BAD.sub(" ", name or "").split())[:100]
    if not name:
        raise TemplateError("Give the template a name.")
    existing = get(user_id, template_id) if template_id else None
    template_id = template_id or uuid.uuid4().hex[:12]
    folder = _folder(user_id, template_id)
    folder.mkdir(parents=True, exist_ok=True)

    attachments = []
    for att in attachment_store.get_many(user_id, attachment_ids):
        file_name = f"att-{uuid.uuid4().hex[:8]}.bin"
        shutil.copyfile(att.path, folder / file_name)
        attachments.append({"file": file_name, "filename": att.filename,
                            "content_type": att.content_type, "size": att.size})

    now = now_iso()
    data = {
        "id": template_id,
        "name": name,
        **{k: compose.get(k) for k in COMPOSE_FIELDS},
        "attachments": attachments,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    tmp = folder / "template.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / "template.json")

    keep = {a["file"] for a in attachments}
    for old in folder.glob("att-*.bin"):  # attachments removed by an update
        if old.name not in keep:
            old.unlink(missing_ok=True)
    return data


def delete(user_id: int, template_id: str) -> None:
    folder = _folder(user_id, template_id)
    if not folder.exists():
        raise TemplateError("Template not found.")
    shutil.rmtree(folder)


def use(user_id: int, template_id: str) -> dict:
    """Load a template into the current email: returns its fields and fresh draft attachments."""
    data = get(user_id, template_id)
    folder = _folder(user_id, template_id)
    attachments = []
    for entry in data.get("attachments", []):
        path = folder / Path(entry["file"]).name
        if path.exists():
            attachments.append(attachment_store.save(user_id, entry["filename"], path.read_bytes()).public())
    return {"compose": {k: data.get(k) for k in COMPOSE_FIELDS}, "attachments": attachments,
            "name": data["name"], "id": data["id"]}
