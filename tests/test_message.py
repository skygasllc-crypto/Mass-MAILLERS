import email
from email import policy

import pytest

from backend.app.attachments import store
from backend.app.sending.message import Compose, MessageError, build_message, message_bytes
from backend.app.smtp.config_store import SmtpConfig

from .conftest import ADMIN_LOGIN, UID  # noqa: F401

PDF = b"%PDF-1.4\n% test pdf\n" + b"0" * 2000

CFG = SmtpConfig(host="smtp.example.com", from_name="Company Team", from_email="newsletter@example.com",
                 reply_to="sales@example.com")


def parse(msg):
    return email.message_from_bytes(message_bytes(msg), policy=policy.default)


def test_required_headers_and_reply_to():
    msg = parse(build_message(CFG, Compose("Important Business Update", "<p>Hello</p>"), []))
    assert msg["From"] == "Company Team <newsletter@example.com>"
    assert msg["To"] == "newsletter@example.com"  # generic visible To, not recipients
    assert msg["Reply-To"] == "sales@example.com"
    assert msg["Subject"] == "Important Business Update"
    for header in ("Date", "Message-ID", "MIME-Version", "Content-Type"):
        assert msg[header], header
    assert msg["Message-ID"].endswith("@example.com>")
    assert "Bcc" not in msg  # recipients live only in the SMTP envelope


def test_configurable_visible_to():
    cfg = SmtpConfig(**{**CFG.__dict__, "to_address": "undisclosed@example.com"})
    assert parse(build_message(cfg, Compose("S", "B", "text"), []))["To"] == "undisclosed@example.com"


def test_html_multipart_structure_with_attachment():
    att = store.save(UID, "company-profile.pdf", PDF)
    msg = parse(build_message(CFG, Compose("S", "<p>Hello <b>World</b></p>"), [(att, att.read())]))
    assert msg.get_content_type() == "multipart/mixed"
    parts = list(msg.iter_parts())
    assert parts[0].get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in parts[0].iter_parts()] == ["text/plain", "text/html"]
    assert parts[1].get_content_type() == "application/pdf"
    assert parts[1].get_filename() == "company-profile.pdf"
    assert parts[1].get_payload(decode=True) == PDF
    assert "Hello World" in msg.get_body(("plain",)).get_content()
    assert "<b>World</b>" in msg.get_body(("html",)).get_content()


def test_plain_text_mode():
    msg = parse(build_message(CFG, Compose("S", "Hello,\n\nBest regards,\nCompany Team", "text"), []))
    assert msg.get_content_type() == "text/plain"
    assert "Best regards," in msg.get_content()


def test_html_mode_without_tags_becomes_paragraphs():
    msg = parse(build_message(CFG, Compose("S", "Hello,\n\nPrices: 5 < 6 & more.\nThanks"), []))
    html = msg.get_body(("html",)).get_content()
    assert "<p>Hello,</p>" in html and "5 &lt; 6 &amp; more" in html and "<br>" in html


def test_personalization_and_escaping():
    compose = Compose("Hi {{name}}", "<p>Hello {{ name }},</p>", personalize=True, name_fallback="there")
    msg = parse(build_message(CFG, compose, [], name="Tom <script>"))
    assert msg["Subject"] == "Hi Tom <script>"
    assert "Hello Tom &lt;script&gt;," in msg.get_body(("html",)).get_content()
    fallback = parse(build_message(CFG, compose, [], name=""))
    assert "Hello there," in fallback.get_body(("html",)).get_content()
    literal = parse(build_message(CFG, Compose("Hi {{name}}", "x"), [], name="Tom"))
    assert literal["Subject"] == "Hi {{name}}"


def test_personalized_message_addressed_to_single_recipient():
    msg = parse(build_message(CFG, Compose("S", "B", personalize=True), [], to_header="john@example.com", name="John"))
    assert msg["To"] == "john@example.com"


def test_header_injection_rejected():
    with pytest.raises(MessageError):
        build_message(CFG, Compose("Hello\r\nBcc: victim@example.com", "B"), [])


def test_unicode_subject_and_name():
    cfg = SmtpConfig(**{**CFG.__dict__, "from_name": "Société Générale"})
    msg = parse(build_message(cfg, Compose("Café ☕ update", "Grüße", "text"), []))
    assert msg["Subject"] == "Café ☕ update"
    assert msg["From"].addresses[0].display_name == "Société Générale"
