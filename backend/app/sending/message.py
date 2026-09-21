"""MIME message construction.

Structure (HTML mode with attachments):
    multipart/mixed
      multipart/alternative
        text/plain
        text/html
      attachment(s)

Recipients are NEVER written into the message headers. BCC recipients only exist in the
SMTP envelope (RCPT TO), so no recipient can see any other recipient.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from ..attachments.store import Attachment
from ..smtp.config_store import SmtpConfig
from ..utils.html_text import html_to_text, looks_like_html, text_to_html, wrap_document

NAME_PLACEHOLDER = re.compile(r"\{\{\s*name\s*\}\}", re.IGNORECASE)
_CRLF = re.compile(r"[\r\n]")


@dataclass
class Compose:
    subject: str
    body: str
    format: str = "html"  # "html" or "text"
    personalize: bool = False
    name_fallback: str = "there"
    individual: bool = False  # one private copy per recipient instead of BCC batches

    @property
    def one_per_recipient(self) -> bool:
        return self.personalize or self.individual


class MessageError(ValueError):
    pass


def uses_placeholder(compose: Compose) -> bool:
    return bool(NAME_PLACEHOLDER.search(compose.subject) or NAME_PLACEHOLDER.search(compose.body))


def _fill(text: str, name: str, escape: bool) -> str:
    value = html.escape(name) if escape else name
    return NAME_PLACEHOLDER.sub(lambda _m: value, text)


def _address(email: str, name: str = "") -> Address:
    local, _, domain = email.partition("@")
    return Address(display_name=name, username=local, domain=domain)


def unsubscribe_header(cfg: SmtpConfig) -> str:
    """RFC 2369 List-Unsubscribe value (mailto, plus the https URL when configured)."""
    parts = []
    if cfg.unsubscribe_mailto:
        parts.append(f"<mailto:{cfg.unsubscribe_mailto}?subject=unsubscribe>")
    if cfg.unsubscribe_url:
        parts.append(f"<{cfg.unsubscribe_url}>")
    return ", ".join(parts)


def footer_text(cfg: SmtpConfig) -> str:
    lines = ["--"]
    if cfg.company_address:
        lines.append(cfg.company_address)
    how = (f"visit {cfg.unsubscribe_url}" if cfg.unsubscribe_url
           else f'reply with "unsubscribe" or email {cfg.unsubscribe_mailto}')
    lines.append(f"To stop receiving these emails, {how}.")
    return "\n".join(lines) + "\n"


def footer_html(cfg: SmtpConfig) -> str:
    address = html.escape(cfg.company_address).replace("\n", "<br>") + "<br>" if cfg.company_address else ""
    if cfg.unsubscribe_url:
        how = f'<a href="{html.escape(cfg.unsubscribe_url)}" style="color:#777;">unsubscribe here</a>'
    else:
        mail = html.escape(cfg.unsubscribe_mailto)
        how = (f'reply with &quot;unsubscribe&quot; or email '
               f'<a href="mailto:{mail}?subject=unsubscribe" style="color:#777;">{mail}</a>')
    return (
        '\n<hr style="border:none;border-top:1px solid #ddd;margin:28px 0 12px;">\n'
        f'<p style="font-size:12px;color:#777;line-height:1.4;">{address}'
        f"To stop receiving these emails, {how}.</p>\n"
    )


def _append_html(document: str, extra: str) -> str:
    match = re.search(r"</body\s*>", document, re.IGNORECASE)
    if match:
        return document[:match.start()] + extra + document[match.start():]
    return document + extra


def build_message(
    cfg: SmtpConfig,
    compose: Compose,
    attachments: list[tuple[Attachment, bytes]],
    *,
    to_header: str | None = None,
    name: str | None = None,
) -> EmailMessage:
    """Build one message.

    ``to_header`` overrides the visible To (used for personalized one-to-one messages).
    ``name`` fills ``{{name}}`` when personalization is enabled.
    """
    for value in (compose.subject, cfg.from_name, cfg.from_email, cfg.reply_to, to_header or "",
                  cfg.unsubscribe_email, cfg.unsubscribe_url):
        if _CRLF.search(value):
            raise MessageError("Header fields cannot contain line breaks.")

    subject, body = compose.subject, compose.body
    if compose.personalize:
        fill_name = (name or "").strip() or compose.name_fallback
        subject = _fill(subject, fill_name, escape=False)
        body = _fill(body, fill_name, escape=compose.format == "html")

    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = _address(cfg.from_email, cfg.from_name)
    msg["To"] = _address(to_header or cfg.visible_to)
    if cfg.reply_to:
        msg["Reply-To"] = _address(cfg.reply_to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.from_email.rpartition("@")[2] or None)
    unsubscribe = unsubscribe_header(cfg)
    if unsubscribe:
        # Lets Gmail, Yahoo, Outlook etc. show their own "Unsubscribe" button.
        msg["List-Unsubscribe"] = unsubscribe
        if cfg.unsubscribe_url:
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"  # RFC 8058

    if compose.format == "html":
        html_body = body if looks_like_html(body) else text_to_html(body)
        document = wrap_document(html_body)
        if cfg.add_footer:
            document = _append_html(document, footer_html(cfg))
        msg.set_content(html_to_text(document))
        msg.add_alternative(document, subtype="html")
    else:
        text = body if body.endswith("\n") else body + "\n"
        if cfg.add_footer:
            text += "\n" + footer_text(cfg)
        msg.set_content(text)

    for att, data in attachments:
        maintype, _, subtype = att.content_type.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=att.filename)
    return msg


# RFC 5322 allows header lines up to 998 characters. Using that limit (instead of 78) prevents
# long List-Unsubscribe URLs from being RFC 2047-encoded, which would make them unusable.
SEND_POLICY = policy.SMTP.clone(max_line_length=998)


def message_bytes(msg: EmailMessage) -> bytes:
    return msg.as_bytes(policy=SEND_POLICY)
