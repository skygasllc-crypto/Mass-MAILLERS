"""Encryption for stored SMTP credentials (Fernet key derived from SECRET_KEY)."""
from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"mass-mailer:smtp-credentials:" + settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        # SECRET_KEY changed: the stored password can no longer be read and must be re-entered.
        return ""


def fingerprint(*parts: str) -> str:
    """Keyed hash used to detect whether SMTP settings changed since the last successful test."""
    msg = "\x00".join(parts).encode("utf-8")
    return hmac.new(settings.secret_key.encode(), msg, hashlib.sha256).hexdigest()
