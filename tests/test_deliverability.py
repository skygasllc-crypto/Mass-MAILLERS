"""Unsubscribe headers/footer, individual delivery, do-not-send list and the DNS deliverability check."""
import email
from email import policy

from backend.app.recipients import suppression
from backend.app.recipients.parser import Recipient
from backend.app.sending.engine import SendRequest, create_job, engine, job_results, prepare
from backend.app.sending.message import Compose, build_message, message_bytes
from backend.app.smtp import deliverability
from backend.app.smtp.config_store import SmtpConfig

from .conftest import ADMIN_LOGIN, UID  # noqa: F401

CFG = SmtpConfig(host="smtp.example.com", username="news@example.com", from_name="Example",
                 from_email="news@example.com", reply_to="sales@example.com")


def parse(msg):
    return email.message_from_bytes(message_bytes(msg), policy=policy.default)


def cfg(**kw):
    return SmtpConfig(**{**CFG.__dict__, **kw})


def test_list_unsubscribe_defaults_to_reply_to():
    msg = parse(build_message(CFG, Compose("S", "<p>B</p>"), []))
    assert msg["List-Unsubscribe"] == "<mailto:sales@example.com?subject=unsubscribe>"
    assert "List-Unsubscribe-Post" not in msg


def test_one_click_unsubscribe_url():
    msg = parse(build_message(cfg(unsubscribe_email="unsub@example.com", unsubscribe_url="https://example.com/u"),
                              Compose("S", "B"), []))
    assert msg["List-Unsubscribe"] == "<mailto:unsub@example.com?subject=unsubscribe>, <https://example.com/u>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_long_unsubscribe_url_is_not_encoded():
    url = "https://example-company.com/unsubscribe?list=newsletter&token=" + "a" * 120
    raw = message_bytes(build_message(cfg(unsubscribe_url=url), Compose("S", "B"), []))
    assert f"<{url}>".encode() in raw and b"=?utf-8?" not in raw


def test_footer_in_html_and_text():
    c = cfg(add_footer=True, company_address="Example Ltd, 1 Main St")
    msg = parse(build_message(c, Compose("S", "<p>Hello</p>"), []))
    html = msg.get_body(("html",)).get_content()
    assert "Example Ltd, 1 Main St" in html and "mailto:sales@example.com?subject=unsubscribe" in html
    assert html.index("unsubscribe") < html.index("</body>")
    assert "To stop receiving these emails" in msg.get_body(("plain",)).get_content()
    plain = parse(build_message(c, Compose("S", "Hello", "text"), []))
    assert plain.get_content().rstrip().endswith('reply with "unsubscribe" or email sales@example.com.')


def test_no_footer_by_default():
    msg = parse(build_message(CFG, Compose("S", "<p>Hello</p>"), []))
    assert "To stop receiving" not in msg.get_body(("html",)).get_content()


def test_individual_delivery_one_copy_each(smtp_server, monkeypatch):
    from backend.app.config import settings
    from backend.app.smtp import config_store
    from backend.app.smtp.client import test_connection

    from .conftest import ADMIN_LOGIN, UID, configure_smtp

    controller, handler = smtp_server
    monkeypatch.setattr(settings, "email_mode", "production")
    c = configure_smtp(controller.port)
    assert test_connection(c)["ok"]
    config_store.mark_verified(c)
    rcpts = [Recipient(f"u{i}@example.org") for i in range(5)]
    req = SendRequest(UID, Compose("S", "B", individual=True), rcpts, 2, "fast", [])
    assert prepare(req)["messages"] == 5 and prepare(req)["delivery"] == "individual"
    engine.start(create_job(req), UID)
    engine.wait(30)
    assert [m["rcpts"] for m in handler.messages] == [[r.email] for r in rcpts]
    for m in handler.messages:
        msg = email.message_from_bytes(m["data"], policy=policy.default)
        assert msg["To"] == m["rcpts"][0]


def test_suppression_and_auto_bounce(auth_client):
    c = auth_client
    c.put("/api/smtp", json={"from_email": "news@example.com"})
    c.post("/api/suppression", json={"text": "Gone@Example.org, bad", "reason": "unsubscribed"})
    res = c.post("/api/recipients/parse", json={"text": "a@example.org gone@example.org"}).json()
    assert res["stats"]["suppressed"] == 1 and res["stats"]["ready"] == 1
    assert res["suppressed"] == ["gone@example.org"] and "gone" not in res["clean_text"]

    payload = {"compose": {"subject": "S", "body": "B"}, "recipients_text": "a@example.org gone@example.org x@bounce.test",
               "speed": "fast", "confirm": True}
    assert c.post("/api/send/prepare", json=payload).json()["recipients"] == 2
    job_id = c.post("/api/send", json=payload).json()["job_id"]
    engine.wait(30)
    assert {r["email"] for r in job_results(job_id)} == {"a@example.org", "x@bounce.test"}
    # a "5.1.1 mailbox unavailable" bounce is added automatically
    assert {r["email"]: r["reason"] for r in c.get("/api/suppression").json()} == {
        "gone@example.org": "unsubscribed", "x@bounce.test": "bounced"}
    c.post("/api/suppression/remove", json={"text": "gone@example.org"})
    assert suppression.all_emails(UID) == {"x@bounce.test"}


def test_unsubscribe_settings_validated(auth_client):
    assert auth_client.put("/api/smtp", json={"unsubscribe_url": "http://insecure.example.com"}).status_code == 422
    assert auth_client.put("/api/smtp", json={"unsubscribe_email": "nope"}).status_code == 422
    ok = auth_client.put("/api/smtp", json={"unsubscribe_url": "https://example.com/u", "add_footer": True})
    assert ok.json()["unsubscribe_url"] == "https://example.com/u" and ok.json()["add_footer"] is True


def test_content_warnings():
    req = SendRequest(UID, Compose("BUY NOW LIMITED OFFER!!", "<p>see https://bit.ly/x</p>"),
                      [Recipient("a@example.org"), Recipient("b@example.org")], 100, "fast", [])
    warnings = " ".join(prepare(req)["warnings"])
    assert "capitals" in warnings and "exclamation" in warnings and "shortener" in warnings
    assert "unsubscribe line" in warnings and "Individual messages" in warnings


def fake_dns(monkeypatch, txt: dict, mx=True):
    monkeypatch.setattr(deliverability, "_txt", lambda name: txt.get(name, []))
    monkeypatch.setattr(deliverability, "_has_mx", lambda domain: mx)


def status(result):
    return {i["name"]: i["status"] for i in result["items"]}


def test_check_all_good(monkeypatch):
    fake_dns(monkeypatch, {"example.com": ["v=spf1 include:_spf.example.net ~all"],
                           "_dmarc.example.com": ["v=DMARC1; p=quarantine"],
                           "selector1._domainkey.example.com": ["v=DKIM1; k=rsa; p=MIGf"]})
    s = status(deliverability.check(cfg(add_footer=True)))
    assert s["SPF"] == s["DMARC"] == s["DKIM"] == s["MX record"] == s["Unsubscribe"] == "ok"


def test_check_missing_records(monkeypatch):
    fake_dns(monkeypatch, {"example.com": ["v=spf1 a", "v=spf1 mx"]}, mx=False)
    s = status(deliverability.check(CFG))
    assert s["SPF"] == "error" and s["DMARC"] == "error" and s["DKIM"] == "warn" and s["MX record"] == "warn"


def test_check_free_mail_through_foreign_server(monkeypatch):
    fake_dns(monkeypatch, {})
    result = deliverability.check(cfg(from_email="someone@gmail.com", host="mail.mycompany.com"))
    assert status(result)["From address"] == "error"
    ok = deliverability.check(cfg(from_email="someone@gmail.com", username="someone@gmail.com", host="smtp.gmail.com"))
    assert status(ok)["From address"] == "ok" and status(ok)["Sending limit"] == "info"
