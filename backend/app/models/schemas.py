"""Request bodies for the JSON API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    email: str = Field(default="", max_length=254)
    password: str = Field(max_length=500)


class RegisterIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=500)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=500)
    new_password: str = Field(max_length=500)


class UserStatusIn(BaseModel):
    status: Literal["active", "disabled", "pending"]


class SmtpSettingsIn(BaseModel):
    host: str = Field(default="", max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str = Field(default="", max_length=320)
    # None / omitted = keep the stored password; "" = clear it.
    password: str | None = Field(default=None, max_length=1000)
    from_name: str = Field(default="", max_length=100)
    from_email: str = Field(default="", max_length=254)
    reply_to: str = Field(default="", max_length=254)
    to_address: str = Field(default="", max_length=254)
    unsubscribe_email: str = Field(default="", max_length=254)
    unsubscribe_url: str = Field(default="", max_length=500)
    company_address: str = Field(default="", max_length=300)
    add_footer: bool = False


class RecipientsIn(BaseModel):
    text: str = Field(default="", max_length=2_000_000)


class ComposeIn(BaseModel):
    subject: str = Field(default="", max_length=900)
    body: str = Field(default="", max_length=2_000_000)
    format: Literal["html", "text"] = "html"
    personalize: bool = False
    name_fallback: str = Field(default="there", max_length=100)


class SendIn(BaseModel):
    compose: ComposeIn
    recipients_text: str = Field(default="", max_length=2_000_000)
    batch_size: int = Field(default=100, ge=1, le=10_000)
    speed: Literal["conservative", "normal", "fast"] = "conservative"
    conservative_delay: int | None = Field(default=None, ge=1, le=3600)  # seconds between batches
    delivery: Literal["bcc", "individual"] = "bcc"
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    confirm: bool = False


class TestSendIn(BaseModel):
    compose: ComposeIn
    delivery: Literal["bcc", "individual"] = "bcc"
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    test_email: str = Field(max_length=254)


class PreviewIn(BaseModel):
    compose: ComposeIn
    recipients_text: str = Field(default="", max_length=2_000_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)


class TemplateIn(BaseModel):
    name: str = Field(max_length=100)
    compose: ComposeIn
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    template_id: str | None = Field(default=None, max_length=12)


class SuppressionIn(BaseModel):
    text: str = Field(default="", max_length=2_000_000)
    reason: Literal["unsubscribed", "bounced", "manual"] = "unsubscribed"
