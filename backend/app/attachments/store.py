"""Attachment upload validation and storage."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import EXECUTABLE_EXTENSIONS, settings
from ..db import get_db, now_iso

# Expected MIME type per extension; the file content is also sniffed below.
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "txt": "text/plain",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "zip": "application/zip",
}

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .doc/.xls container
_ZIP = b"PK\x03\x04"
MAGIC = {
    "pdf": [b"%PDF"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "gif": [b"GIF87a", b"GIF89a"],
    "zip": [_ZIP, b"PK\x05\x06"],
    "docx": [_ZIP],
    "xlsx": [_ZIP],
    "doc": [_OLE],
    "xls": [_OLE],
}
_EXECUTABLE_MAGIC = [b"MZ", b"\x7fELF", b"#!"]


class AttachmentError(ValueError):
    pass


@dataclass
class Attachment:
    id: str
    filename: str
    size: int
    content_type: str
    path: Path

    def public(self) -> dict:
        return {"id": self.id, "filename": self.filename, "size": self.size, "content_type": self.content_type}

    def read(self) -> bytes:
        return self.path.read_bytes()


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]", "_", name).strip(" .")
    return name[:150] or "attachment"


def extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def validate(filename: str, data: bytes) -> str:
    """Validate an upload; returns the MIME type to use."""
    ext = extension(filename)
    max_bytes = int(settings.max_attachment_mb * 1024 * 1024)
    if not data:
        raise AttachmentError("The file is empty.")
    if len(data) > max_bytes:
        raise AttachmentError(f"File is larger than the {settings.max_attachment_mb:g} MB attachment limit.")

    # Text files are checked for binary content below instead of by magic bytes.
    sniff = ext not in {"csv", "txt"}
    executable = ext in EXECUTABLE_EXTENSIONS or (sniff and any(data.startswith(m) for m in _EXECUTABLE_MAGIC))
    if executable:
        if not settings.allow_executable_attachments:
            raise AttachmentError("Executable files cannot be attached.")
        return "application/octet-stream"

    if ext not in settings.allowed_extensions:
        allowed = ", ".join(sorted(settings.allowed_extensions))
        raise AttachmentError(f"File type .{ext or '?'} is not allowed. Allowed: {allowed}.")

    signatures = MAGIC.get(ext)
    if signatures and not any(data.startswith(sig) for sig in signatures):
        raise AttachmentError(f"The file content does not look like a valid .{ext} file.")
    if ext in {"csv", "txt"} and b"\x00" in data[:4096]:
        raise AttachmentError(f"The file content does not look like a valid .{ext} file.")
    return CONTENT_TYPES.get(ext, "application/octet-stream")


def save(user_id: int, filename: str, data: bytes) -> Attachment:
    filename = safe_filename(filename)
    content_type = validate(filename, data)
    att_id = uuid.uuid4().hex
    stored_name = att_id + ".bin"  # never use the user-supplied name on disk
    path = settings.upload_dir / stored_name
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO attachments(id, filename, stored_name, size, content_type, created_at, user_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (att_id, filename, stored_name, len(data), content_type, now_iso(), user_id),
        )
    return Attachment(att_id, filename, len(data), content_type, path)


def _row_to_attachment(row) -> Attachment:
    return Attachment(row["id"], row["filename"], row["size"], row["content_type"],
                      settings.upload_dir / row["stored_name"])


def list_all(user_id: int) -> list[Attachment]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM attachments WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()
    return [_row_to_attachment(r) for r in rows]


def get_many(user_id: int, ids: list[str]) -> list[Attachment]:
    found = []
    with get_db() as conn:
        for att_id in ids:
            row = conn.execute("SELECT * FROM attachments WHERE id=? AND user_id=?", (att_id, user_id)).fetchone()
            if row is None:
                raise AttachmentError("An attachment was not found. Please re-attach the file.")
            found.append(_row_to_attachment(row))
    return found


def delete(user_id: int, att_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT stored_name FROM attachments WHERE id=? AND user_id=?", (att_id, user_id)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM attachments WHERE id=?", (att_id,))
    (settings.upload_dir / row["stored_name"]).unlink(missing_ok=True)
    return True
