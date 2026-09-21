import pytest

from backend.app.attachments import store
from backend.app.config import settings

from .conftest import ADMIN_LOGIN, UID  # noqa: F401


def test_valid_files_accepted():
    cases = {
        "doc.pdf": b"%PDF-1.7 content",
        "img.png": b"\x89PNG\r\n\x1a\n rest",
        "photo.jpg": b"\xff\xd8\xff\xe0 rest",
        "archive.zip": b"PK\x03\x04 rest",
        "report.docx": b"PK\x03\x04 rest",
        "sheet.xlsx": b"PK\x03\x04 rest",
        "old.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 rest",
        "data.csv": b"a,b\n1,2\n",
    }
    for name, data in cases.items():
        att = store.save(UID, name, data)
        assert att.filename == name
        assert att.path.read_bytes() == data
    assert len(store.list_all(UID)) == len(cases)


@pytest.mark.parametrize("name", ["setup.exe", "run.bat", "run.cmd", "screen.scr", "script.ps1"])
def test_executables_blocked(name):
    with pytest.raises(store.AttachmentError, match="Executable"):
        store.save(UID, name, b"MZ\x90\x00 payload")


def test_disguised_executable_blocked():
    with pytest.raises(store.AttachmentError):
        store.save(UID, "invoice.pdf", b"MZ\x90\x00 payload")


def test_executables_allowed_by_admin(monkeypatch):
    monkeypatch.setattr(settings, "allow_executable_attachments", True)
    assert store.save(UID, "tool.exe", b"MZ\x90\x00").content_type == "application/octet-stream"


def test_content_mismatch_and_unknown_type():
    with pytest.raises(store.AttachmentError, match="does not look like"):
        store.save(UID, "fake.pdf", b"just text")
    with pytest.raises(store.AttachmentError, match="not allowed"):
        store.save(UID, "page.html", b"<html></html>")


def test_size_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_attachment_mb", 0.001)  # ~1 KB
    with pytest.raises(store.AttachmentError, match="larger"):
        store.save(UID, "big.pdf", b"%PDF" + b"0" * 5000)


def test_path_traversal_filename_sanitized():
    att = store.save(UID, "../../etc/evil.pdf", b"%PDF-1.4")
    assert att.filename == "evil.pdf"
    assert att.path.parent == settings.upload_dir


def test_delete_removes_file():
    att = store.save(UID, "a.pdf", b"%PDF-1.4")
    assert store.delete(UID, att.id)
    assert not att.path.exists()
    assert store.list_all(UID) == []
