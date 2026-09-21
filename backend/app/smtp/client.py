"""SMTP connections (smtplib), the connection test, and the development mailbox transport."""
from __future__ import annotations

import ipaddress
import json
import logging
import smtplib
import socket
import ssl

from ..config import settings
from ..db import get_db, now_iso
from .config_store import SmtpConfig

log = logging.getLogger("mailer.smtp")

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
SMTP_PORTS = {25, 465, 587, 2525}


class SmtpSetupError(Exception):
    """Configuration or connection problem with a user-friendly message."""


def tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not settings.smtp_tls_verify:  # only for local test servers with self-signed certificates
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def host_problem(cfg: SmtpConfig) -> str | None:
    """Stop regular users from pointing the server at internal network addresses or non-mail ports.

    Without this, anyone who registers could use "Test SMTP Connection" to probe the local network.
    Admins (and ALLOW_PRIVATE_SMTP_HOSTS=true) are exempt.
    """
    if cfg.trusted or settings.allow_private_smtp_hosts:
        return None
    if cfg.port not in SMTP_PORTS:
        return f"Use a standard SMTP port ({', '.join(map(str, sorted(SMTP_PORTS)))})."
    try:
        infos = socket.getaddrinfo(cfg.host, cfg.port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return None  # reported by the DNS step / connection attempt
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return "This SMTP host points to a private or local network address. Use your provider's public SMTP server."
    return None


def connect(cfg: SmtpConfig) -> smtplib.SMTP:
    """Open an authenticated SMTP connection according to ``cfg``."""
    problem = host_problem(cfg)
    if problem:
        raise SmtpSetupError(problem)
    timeout = settings.smtp_timeout
    if cfg.security == "ssl":
        conn: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=timeout, context=tls_context())
        conn.ehlo()
    else:
        conn = smtplib.SMTP(cfg.host, cfg.port, timeout=timeout)
        conn.ehlo()
        if cfg.security == "starttls":
            if not conn.has_extn("starttls"):
                conn.close()
                raise SmtpSetupError("The server does not support STARTTLS on this port.")
            conn.starttls(context=tls_context())
            conn.ehlo()
    if cfg.username:
        if cfg.security == "none" and cfg.host.lower() not in LOCAL_HOSTS:
            conn.close()
            raise SmtpSetupError("Refusing to send the password over an unencrypted connection. Use STARTTLS or SSL/TLS.")
        conn.login(cfg.username, cfg.password)
    return conn


def config_problems(cfg: SmtpConfig) -> list[str]:
    problems = []
    if not cfg.host:
        problems.append("SMTP host is required.")
    if not (1 <= cfg.port <= 65535):
        problems.append("SMTP port must be between 1 and 65535.")
    if cfg.security not in {"starttls", "ssl", "none"}:
        problems.append("Unknown security option.")
    if not settings.is_development:
        if not cfg.username or not cfg.password:
            problems.append("Production mode requires an SMTP username and password.")
        if not cfg.from_email:
            problems.append("Production mode requires a From email address.")
    return problems


def test_connection(cfg: SmtpConfig) -> dict:
    """Check DNS, TCP, TLS and authentication. Messages never contain the password or raw server output."""
    steps: list[dict] = []

    def step(name: str, ok: bool | None, message: str) -> None:
        steps.append({"name": name, "ok": ok, "message": message})

    def result() -> dict:
        ok = all(s["ok"] is not False for s in steps)
        return {"ok": ok, "steps": steps,
                "message": "Connection successful" if ok else next(s["message"] for s in steps if s["ok"] is False)}

    problems = config_problems(cfg)
    if problems:
        step("Settings", False, problems[0])
        return result()

    try:
        socket.getaddrinfo(cfg.host, cfg.port, type=socket.SOCK_STREAM)
        step("DNS resolution", True, "Host name resolved")
    except (socket.gaierror, UnicodeError):
        step("DNS resolution", False, "Could not resolve the SMTP host name")
        return result()

    problem = host_problem(cfg)
    if problem:
        step("SMTP host", False, problem)
        return result()

    try:
        socket.create_connection((cfg.host, cfg.port), timeout=10).close()
        step("TCP connection", True, f"Port {cfg.port} is reachable")
    except OSError:
        step("TCP connection", False, f"Could not connect to port {cfg.port} (blocked, closed, or wrong port)")
        return result()

    timeout = min(settings.smtp_timeout, 20)
    conn = None
    try:
        if cfg.security == "ssl":
            conn = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=timeout, context=tls_context())
            conn.ehlo()
            step("TLS negotiation", True, "SSL/TLS connection established")
        else:
            conn = smtplib.SMTP(cfg.host, cfg.port, timeout=timeout)
            conn.ehlo()
            if cfg.security == "starttls":
                if not conn.has_extn("starttls"):
                    step("TLS negotiation", False, "Server does not offer STARTTLS on this port")
                    return result()
                conn.starttls(context=tls_context())
                conn.ehlo()
                step("TLS negotiation", True, "STARTTLS encryption established")
            else:
                step("TLS negotiation", None, "Skipped - connection is NOT encrypted")
    except ssl.SSLCertVerificationError:
        step("TLS negotiation", False, "TLS certificate could not be verified")
        return result()
    except (ssl.SSLError, smtplib.SMTPException, OSError):
        hint = " (port 465 normally uses SSL/TLS, 587 uses STARTTLS)" if cfg.port in (465, 587) else ""
        step("TLS negotiation", False, "Secure connection failed" + hint)
        return result()

    try:
        if not cfg.username:
            step("Authentication", None, "Skipped - no username configured")
        elif cfg.security == "none" and cfg.host.lower() not in LOCAL_HOSTS:
            step("Authentication", False, "Refusing to send the password over an unencrypted connection")
        elif not conn.has_extn("auth"):
            step("Authentication", False, "Server does not offer authentication on this connection")
        else:
            conn.login(cfg.username, cfg.password)
            step("Authentication", True, "Signed in successfully")
    except smtplib.SMTPAuthenticationError:
        step("Authentication", False, "Authentication failed")
    except (smtplib.SMTPException, OSError):
        step("Authentication", False, "Authentication could not be completed")
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    return result()


class SmtpTransport:
    """Sends via the configured SMTP server. The connection is opened per batch."""

    def __init__(self, cfg: SmtpConfig) -> None:
        self.cfg = cfg
        self.conn: smtplib.SMTP | None = None

    def open(self) -> None:
        self.close()
        self.conn = connect(self.cfg)

    def send(self, envelope_from: str, recipients: list[str], data: bytes) -> dict:
        """Returns refused recipients as {email: (code, message)}; raises for whole-message failures."""
        if self.conn is None:
            self.open()
        return self.conn.sendmail(envelope_from, recipients, data)

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.quit()
            except Exception:
                try:
                    self.conn.close()
                except Exception:
                    pass
            self.conn = None


class DevTransport:
    """EMAIL_MODE=development: nothing leaves the machine. Messages go to the local test mailbox.

    Reserved test domains simulate SMTP failures so the failure/retry paths can be tried out:
      *@bounce.test    -> 550 permanent failure
      *@tempfail.test  -> 451 temporary failure on every attempt
      *@retry.test     -> 451 on the first attempt, accepted on retry
    """

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._attempts: dict[str, int] = {}

    def open(self) -> None:
        pass

    def send(self, envelope_from: str, recipients: list[str], data: bytes) -> dict:
        refused = {}
        accepted = []
        for rcpt in recipients:
            domain = rcpt.rsplit("@", 1)[-1]
            self._attempts[rcpt] = self._attempts.get(rcpt, 0) + 1
            if domain == "bounce.test":
                refused[rcpt] = (550, b"5.1.1 Mailbox unavailable")
            elif domain == "tempfail.test" or (domain == "retry.test" and self._attempts[rcpt] == 1):
                refused[rcpt] = (451, b"4.3.0 Temporary local problem, try again later")
            else:
                accepted.append(rcpt)
        if not accepted:
            raise smtplib.SMTPRecipientsRefused(refused)
        store_dev_message(self.user_id, envelope_from, accepted, data)
        return refused

    def close(self) -> None:
        pass


def store_dev_message(user_id: int, envelope_from: str, recipients: list[str], data: bytes) -> None:
    import email
    from email import policy

    msg = email.message_from_bytes(data, policy=policy.default)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO dev_outbox(created_at, envelope_from, envelope_to, subject, raw, user_id) VALUES(?,?,?,?,?,?)",
            (now_iso(), envelope_from, json.dumps(recipients), str(msg.get("Subject", "")),
             data.decode("utf-8", errors="replace"), user_id),
        )


def get_transport(cfg: SmtpConfig):
    return DevTransport(cfg.user_id) if settings.is_development else SmtpTransport(cfg)
