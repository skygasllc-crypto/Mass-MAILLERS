"""Background sending engine.

Recipients -> validation -> batching -> SMTP connection -> send batch -> record result -> next batch

A single worker thread processes one job at a time so the web UI stays responsive.
Each BCC batch is ONE message whose recipients exist only in the SMTP envelope.
With personalization enabled, each recipient in a batch receives their own message
addressed only to them (still one SMTP connection per batch).
"""
from __future__ import annotations

import json
import logging
import re
import smtplib
import threading
import uuid
from dataclasses import asdict, dataclass

from ..attachments.store import AttachmentError, get_many
from ..config import CONSERVATIVE_DELAY_CHOICES, settings
from ..db import get_db, now_iso
from ..recipients import suppression
from ..recipients.parser import Recipient, is_valid_email, make_batches
from ..smtp import config_store
from ..smtp.client import SmtpSetupError, config_problems, get_transport
from .message import Compose, MessageError, build_message, message_bytes, uses_placeholder

log = logging.getLogger("mailer.engine")


@dataclass
class SendRequest:
    user_id: int
    compose: Compose
    recipients: list[Recipient]
    batch_size: int
    speed: str
    attachment_ids: list[str]
    conservative_delay: int | None = None  # user-chosen pause for conservative mode, in seconds


def batch_delay(speed: str, conservative_delay: int | None = None) -> float:
    """Seconds to pause between batches for this sending mode. A pause the user picked is used exactly,
    also in development mode (only the preset pauses are shortened there)."""
    if speed == "conservative" and conservative_delay in CONSERVATIVE_DELAY_CHOICES:
        return float(conservative_delay)
    return settings.speed_delays.get(speed, settings.speed_delays["conservative"])


class FatalSendError(Exception):
    """Stops the whole job (e.g. authentication rejected)."""


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:g}s"
    minutes = seconds / 60
    return f"{minutes:g} min" if minutes < 60 else f"{minutes / 60:g} h"


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.2f} MB"


def check_sender(cfg: config_store.SmtpConfig) -> list[str]:
    errors = []
    if not cfg.from_email:
        errors.append("Enter a From email address in SMTP settings.")
    elif not is_valid_email(cfg.from_email.lower()):
        errors.append("The From email address is not valid.")
    if cfg.reply_to and not is_valid_email(cfg.reply_to.lower()):
        errors.append("The Reply-To address is not valid.")
    if cfg.to_address and not is_valid_email(cfg.to_address.lower()):
        errors.append("The visible To address is not valid.")
    if not settings.is_development:
        problems = config_problems(cfg)
        if problems:
            errors.extend(problems)
        elif not config_store.is_verified(cfg):
            errors.append("Production mode: run 'Test SMTP Connection' successfully before sending.")
    return errors


def sender_warnings(cfg: config_store.SmtpConfig) -> list[str]:
    warnings = []
    if "@" in cfg.username and cfg.from_email and cfg.username.lower() != cfg.from_email.lower():
        if cfg.username.rpartition("@")[2].lower() != cfg.from_email.rpartition("@")[2].lower():
            warnings.append(
                "The From address is on a different domain than the SMTP account. Only use a From "
                "address you are authorized to send as, or the message may be rejected."
            )
    return warnings


_SHORTENERS = re.compile(r"https?://(bit\.ly|tinyurl\.com|goo\.gl|t\.co|ow\.ly|is\.gd|buff\.ly|cutt\.ly|rb\.gy)/", re.I)
# Enhanced status codes meaning "this address does not exist" -> never send to it again.
_BAD_MAILBOX = re.compile(r"^5\d\d\s+5\.1\.(1|2|3|10)\b")


def content_warnings(cfg: config_store.SmtpConfig, compose: Compose, recipients: int) -> list[str]:
    """Things that commonly push mail into spam at Gmail, Yahoo and Outlook."""
    warnings = []
    letters = re.sub(r"[^A-Za-z]", "", compose.subject)
    if len(letters) >= 8 and letters.isupper():
        warnings.append("The subject is written in capitals, which spam filters penalize.")
    if compose.subject.count("!") >= 2:
        warnings.append("Several exclamation marks in the subject look promotional to spam filters.")
    if _SHORTENERS.search(compose.body):
        warnings.append("The message uses a URL shortener (bit.ly etc.); these are often flagged. Use full links.")
    if compose.format == "html" and "<img" in compose.body.lower():
        text = re.sub(r"<[^>]+>", " ", compose.body)
        if len(text.split()) < 40:
            warnings.append("The email is mostly images with little text; add some text for better inbox placement.")
    if not cfg.add_footer and not cfg.unsubscribe_url:
        warnings.append("No unsubscribe line in the email. Turn on the footer (SMTP settings → Deliverability) "
                        "so people unsubscribe instead of marking the email as spam.")
    if not compose.one_per_recipient and recipients > 1:
        warnings.append("Tip: 'Individual messages' delivery usually gets better inbox placement at Gmail, "
                        "Yahoo and Outlook than BCC batches.")
    return warnings


def prepare(req: SendRequest, *, test: bool = False) -> dict:
    """Validate a send request and return a summary for the confirmation dialog."""
    cfg = config_store.load(req.user_id)
    errors = check_sender(cfg)
    warnings = sender_warnings(cfg)
    compose = req.compose

    if not compose.subject.strip():
        errors.append("Enter a subject.")
    elif len(compose.subject) > 900:
        errors.append("The subject is too long.")
    if not compose.body.strip():
        errors.append("Write a message.")
    if compose.format not in {"html", "text"}:
        errors.append("Unknown message format.")
    if not test:
        if not req.recipients:
            errors.append("Add at least one valid recipient.")
        if len(req.recipients) > settings.max_recipients:
            errors.append(f"Too many recipients: the limit is {settings.max_recipients:,}.")
        if not 1 <= req.batch_size <= settings.max_batch_size:
            errors.append(f"BCC batch size must be between 1 and {settings.max_batch_size}.")
        if req.speed not in settings.speed_delays:
            errors.append("Choose a sending speed.")
        if req.speed == "conservative" and req.conservative_delay is not None \
                and req.conservative_delay not in CONSERVATIVE_DELAY_CHOICES:
            errors.append("Choose a conservative pause of 10s, 30s, 1m, 2m, 5m, 10m or 20m.")

    warnings.extend(content_warnings(cfg, compose, len(req.recipients)))
    if compose.personalize and not uses_placeholder(compose):
        warnings.append("Personalization is on, but the message does not contain {{name}}.")
    if not compose.personalize and uses_placeholder(compose):
        warnings.append("The message contains {{name}} but personalization is off - it will be sent literally.")

    size = 0
    attachments = []
    try:
        attachments = get_many(req.user_id, req.attachment_ids)
    except AttachmentError as exc:
        errors.append(str(exc))
    if not errors:
        try:
            sample_name = next((r.name for r in req.recipients if r.name), None)
            msg = build_message(cfg, compose, [(a, a.read()) for a in attachments], name=sample_name)
            size = len(message_bytes(msg))
        except (MessageError, ValueError) as exc:
            errors.append(str(exc))
        max_bytes = int(settings.max_message_mb * 1024 * 1024)
        if size > max_bytes:
            errors.append(f"The email is too large ({_mb(size)}). The limit is {settings.max_message_mb:g} MB.")

    batch_size = max(1, min(req.batch_size, settings.max_batch_size))
    total_batches = (len(req.recipients) + batch_size - 1) // batch_size
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "recipients": len(req.recipients),
        "batches": total_batches,
        "batch_size": batch_size,
        "messages": len(req.recipients) if compose.one_per_recipient else total_batches,
        "delivery": "individual" if compose.one_per_recipient else "bcc",
        "from": f"{cfg.from_name} <{cfg.from_email}>" if cfg.from_name else cfg.from_email,
        "reply_to": cfg.reply_to or cfg.from_email,
        "visible_to": cfg.visible_to,
        "subject": compose.subject,
        "attachments": [a.filename for a in attachments],
        "size_bytes": size,
        "size": _mb(size),
        "speed": req.speed,
        "delay_seconds": batch_delay(req.speed, req.conservative_delay),
        "email_mode": settings.email_mode,
    }


def _friendly_smtp_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "Authentication failed"
    if isinstance(exc, SmtpSetupError):
        return str(exc)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        code, resp = next(iter(exc.recipients.values()))
        return f"Recipient refused: {code} {_decode(resp)}"
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"{exc.smtp_code} {_decode(exc.smtp_error)}"
    if isinstance(exc, (OSError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return "Could not connect to the SMTP server"
    return "SMTP error"


def _decode(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return " ".join(str(value).split())[:300]


def send_test(user_id: int, compose: Compose, attachment_ids: list[str], test_email: str) -> dict:
    """Send the exact campaign message (same From, Reply-To, Subject, body, attachments) to one address."""
    test_email = test_email.strip().lower()
    if not is_valid_email(test_email):
        return {"ok": False, "message": "Enter a valid test email address."}
    summary = prepare(SendRequest(user_id, compose, [], settings.default_batch_size, "normal", attachment_ids), test=True)
    if not summary["ok"]:
        return {"ok": False, "message": summary["errors"][0], "errors": summary["errors"]}

    cfg = config_store.load(user_id)
    attachments = [(a, a.read()) for a in get_many(user_id, attachment_ids)]
    to_header = test_email if compose.one_per_recipient else None
    msg = build_message(cfg, compose, attachments, to_header=to_header)
    transport = get_transport(cfg)
    try:
        transport.open()
        refused = transport.send(cfg.from_email, [test_email], message_bytes(msg))
        if refused:
            code, resp = refused[test_email]
            return {"ok": False, "message": f"Test address refused: {code} {_decode(resp)}"}
    except (smtplib.SMTPException, SmtpSetupError, OSError) as exc:
        log.warning("Test email failed: %s", type(exc).__name__)
        return {"ok": False, "message": "Test email failed: " + _friendly_smtp_error(exc)}
    finally:
        transport.close()
    where = "the development mailbox" if settings.is_development else test_email
    return {"ok": True, "message": f"Test email sent to {where}.", "size": summary["size"]}


# --------------------------------------------------------------------------- jobs

def create_job(req: SendRequest) -> str:
    cfg = config_store.load(req.user_id)
    batch_size = max(1, min(req.batch_size, settings.max_batch_size))
    batches = make_batches(req.recipients, batch_size)
    job_id = uuid.uuid4().hex[:12]
    payload = {"compose": asdict(req.compose), "attachment_ids": req.attachment_ids,
               "conservative_delay": req.conservative_delay}
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO jobs(id, created_at, status, status_text, subject, from_email, reply_to, email_mode, "
            "total, batch_size, total_batches, current_batch, speed, payload, user_id, smtp_account, smtp_enc) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, now, "queued", "Waiting to start...", req.compose.subject, cfg.from_email,
             cfg.reply_to or cfg.from_email, settings.email_mode, len(req.recipients), batch_size,
             len(batches), 0, req.speed, json.dumps(payload), req.user_id, cfg.account, config_store.snapshot(cfg)),
        )
        conn.executemany(
            "INSERT INTO job_recipients(job_id, email, name, batch_no, status, updated_at) VALUES(?,?,?,?,?,?)",
            [(job_id, r.email, r.name, i, "pending", now) for i, batch in enumerate(batches, 1) for r in batch],
        )
    return job_id


def job_progress(job_id: str, user_id: int | None = None) -> dict | None:
    """Progress of a job; with ``user_id`` only if that user owns it."""
    with get_db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is not None and user_id is not None and job["user_id"] != user_id:
            job = None
        if job is None:
            return None
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM job_recipients WHERE job_id=? GROUP BY status", (job_id,)
        ).fetchall())
    sent, failed = counts.get("sent", 0), counts.get("failed", 0)
    not_sent = counts.get("not_sent", 0)
    total = job["total"]
    done = sent + failed + not_sent
    return {
        "id": job["id"],
        "status": job["status"],
        "status_text": job["status_text"],
        "subject": job["subject"],
        "from_email": job["from_email"],
        "reply_to": job["reply_to"],
        "smtp_account": job["smtp_account"] or job["from_email"],
        "email_mode": job["email_mode"],
        "total": total,
        "completed": done,
        "successful": sent,
        "failed": failed,
        "not_sent": not_sent,
        "pending": counts.get("pending", 0),
        "percent": round(100 * done / total) if total else 100,
        "current_batch": job["current_batch"],
        "total_batches": job["total_batches"],
        "batch_size": job["batch_size"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "finished": job["status"] in {"completed", "cancelled", "failed", "interrupted"},
    }


def latest_job_id(user_id: int) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                           (user_id,)).fetchone()
    return row["id"] if row else None


def active_job_ids(user_id: int) -> list[str]:
    with get_db() as conn:
        rows = conn.execute("SELECT id FROM jobs WHERE user_id=? AND status IN ('queued', 'running') "
                            "ORDER BY created_at, rowid", (user_id,)).fetchall()
    return [r["id"] for r in rows]


def attachments_in_use(user_id: int) -> set[str]:
    """Attachment ids used by this user's sends that have not finished yet."""
    with get_db() as conn:
        rows = conn.execute("SELECT payload FROM jobs WHERE user_id=? AND status IN ('queued', 'running')",
                            (user_id,)).fetchall()
    return {a for r in rows for a in json.loads(r["payload"])["attachment_ids"]}


def job_results(job_id: str, status: str | None = None) -> list[dict]:
    query = "SELECT email, name, batch_no, status, smtp_response, attempts, updated_at FROM job_recipients WHERE job_id=?"
    params: list = [job_id]
    if status == "failed":
        query += " AND status IN ('failed', 'not_sent')"
    elif status:
        query += " AND status=?"
        params.append(status)
    with get_db() as conn:
        rows = conn.execute(query + " ORDER BY id", params).fetchall()
    return [dict(r) for r in rows]


class _Worker:
    def __init__(self, job_id: str, user_id: int, account_key: str, thread: threading.Thread) -> None:
        self.job_id = job_id
        self.user_id = user_id
        self.account_key = account_key
        self.thread = thread
        self.cancel = threading.Event()


class SendEngine:
    """Runs sends in background threads, independent of the browser session (logging out does not stop them).

    A user may run several sends at once, each through a different SMTP account; one SMTP account is
    never used by two sends at the same time, so its sending pace and provider limits are respected.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, _Worker] = {}  # job_id -> worker
        self._local = threading.local()

    @property
    def _cancel(self) -> threading.Event:
        return self._local.cancel

    def _alive(self) -> list[_Worker]:
        self._workers = {k: w for k, w in self._workers.items() if w.thread.is_alive()}
        return list(self._workers.values())

    # -- control

    def busy(self, user_id: int | None = None) -> bool:
        with self._lock:
            return any(user_id is None or w.user_id == user_id for w in self._alive())

    def check_can_start(self, user_id: int, account_key: str) -> None:
        """Raise RuntimeError with a user-facing reason if a new send cannot start now."""
        with self._lock:
            self._check(user_id, account_key)

    def _check(self, user_id: int, account_key: str) -> None:
        alive = self._alive()
        if any(w.account_key == account_key for w in alive):
            raise RuntimeError("This SMTP account is already sending. Wait for that send to finish, "
                               "or enter a different SMTP account.")
        if sum(w.user_id == user_id for w in alive) >= settings.max_parallel_sends:
            raise RuntimeError(f"You can run at most {settings.max_parallel_sends} sends at the same time.")

    def start(self, job_id: str, user_id: int, account_key: str) -> None:
        with self._lock:
            self._check(user_id, account_key)
            thread = threading.Thread(target=self._run, args=(job_id,), name=f"send-{job_id}", daemon=True)
            self._workers[job_id] = _Worker(job_id, user_id, account_key, thread)
            thread.start()

    def discard(self, job_id: str, reason: str) -> None:
        """Close a job that was created but could not be started."""
        self._mark_remaining(job_id, "Not sent (not started)")
        self._update_job(job_id, status="failed", status_text=f"Not started: {reason}", finished_at=now_iso(),
                         smtp_enc=None)

    def cancel(self, job_id: str, user_id: int) -> bool:
        with self._lock:
            worker = self._workers.get(job_id)
        if worker is not None and worker.user_id == user_id and worker.thread.is_alive():
            worker.cancel.set()
            return True
        return False

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            threads = [w.thread for w in self._workers.values()]
        for thread in threads:
            thread.join(timeout)

    # -- helpers

    def _update_job(self, job_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with get_db() as conn:
            conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def _mark(self, job_id: str, emails: list[str], status: str, response: str, attempts: int) -> None:
        if not emails:
            return
        now = now_iso()
        with get_db() as conn:
            conn.executemany(
                "UPDATE job_recipients SET status=?, smtp_response=?, attempts=?, updated_at=? "
                "WHERE job_id=? AND email=?",
                [(status, response[:500], attempts, now, job_id, e) for e in emails],
            )

    def _mark_remaining(self, job_id: str, response: str) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE job_recipients SET status='not_sent', smtp_response=?, updated_at=? "
                "WHERE job_id=? AND status='pending'",
                (response, now_iso(), job_id),
            )

    # -- worker

    def _run(self, job_id: str) -> None:
        with self._lock:
            self._local.cancel = self._workers[job_id].cancel
        try:
            self._process(job_id)
        except Exception:  # never leave a job stuck in "running"
            log.exception("Job %s crashed", job_id)
            self._mark_remaining(job_id, "Not sent (internal error)")
            self._update_job(job_id, status="failed", status_text="Stopped because of an internal error.",
                             finished_at=now_iso())
        finally:
            self._update_job(job_id, smtp_enc=None)  # don't keep a copy of the credentials once finished

    def _process(self, job_id: str) -> None:
        with get_db() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        payload = json.loads(job["payload"])
        compose = Compose(**payload["compose"])
        user_id = job["user_id"]
        # The SMTP settings the send was started with; later changes in the form don't affect it.
        cfg = (job["smtp_enc"] and config_store.from_snapshot(job["smtp_enc"])) or config_store.load(user_id)
        attachments = [(a, a.read()) for a in get_many(user_id, payload["attachment_ids"])]
        delay = batch_delay(job["speed"], payload.get("conservative_delay"))
        message_delay = settings.message_delays.get(job["speed"], settings.message_delays["conservative"])
        total_batches = job["total_batches"]
        transport = get_transport(cfg)

        self._update_job(job_id, status="running", started_at=now_iso(), status_text="Connecting...")
        log.info("Job %s started: %d recipients in %d batches", job_id, job["total"], total_batches)

        try:
            for batch_no in range(1, total_batches + 1):
                if self._cancel.is_set():
                    break
                self._update_job(job_id, current_batch=batch_no,
                                 status_text=f"Sending batch {batch_no} of {total_batches}...")
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT email, name FROM job_recipients WHERE job_id=? AND batch_no=? AND status='pending' "
                        "ORDER BY id", (job_id, batch_no),
                    ).fetchall()
                if not rows:
                    continue

                self._open_with_retry(transport, job_id, batch_no, total_batches)

                if compose.one_per_recipient:
                    for index, row in enumerate(rows):
                        if self._cancel.is_set():
                            break
                        if index and message_delay:
                            self._cancel.wait(message_delay)  # stay within per-minute provider limits
                        msg = build_message(cfg, compose, attachments, to_header=row["email"], name=row["name"])
                        self._deliver(transport, job_id, user_id, cfg.from_email, [row["email"]], message_bytes(msg))
                else:
                    msg = build_message(cfg, compose, attachments)
                    self._deliver(transport, job_id, user_id, cfg.from_email, [r["email"] for r in rows],
                                  message_bytes(msg))

                transport.close()
                log.info("Job %s: batch %d/%d done", job_id, batch_no, total_batches)
                if batch_no < total_batches and not self._cancel.is_set():
                    self._update_job(job_id, status_text=f"Batch {batch_no} sent. Waiting {_duration(delay)} before the next batch...")
                    self._cancel.wait(delay)
        except FatalSendError as exc:
            transport.close()
            self._mark_remaining(job_id, f"Not sent: {exc}")
            self._update_job(job_id, status="failed", status_text=f"Stopped: {exc}", finished_at=now_iso())
            log.warning("Job %s stopped: %s", job_id, exc)
            return
        finally:
            transport.close()

        if self._cancel.is_set():
            self._mark_remaining(job_id, "Not sent (cancelled by user)")
            self._update_job(job_id, status="cancelled", status_text="Sending was cancelled.", finished_at=now_iso())
            log.info("Job %s cancelled", job_id)
        else:
            self._update_job(job_id, status="completed", status_text="Sending complete.", finished_at=now_iso())
            log.info("Job %s completed", job_id)

    def _open_with_retry(self, transport, job_id: str, batch_no: int, total_batches: int) -> None:
        for attempt in range(1, settings.max_retries + 2):
            try:
                transport.open()
                return
            except smtplib.SMTPAuthenticationError as exc:
                raise FatalSendError("SMTP authentication failed") from exc
            except SmtpSetupError as exc:
                raise FatalSendError(str(exc)) from exc
            except (smtplib.SMTPException, OSError) as exc:
                log.warning("Job %s: connection attempt %d failed (%s)", job_id, attempt, type(exc).__name__)
                if attempt > settings.max_retries or self._cancel.is_set():
                    raise FatalSendError("could not connect to the SMTP server") from exc
                self._update_job(job_id, status_text=f"Connection problem - retrying batch {batch_no} of {total_batches}...")
                self._cancel.wait(settings.retry_delay)

    def _deliver(self, transport, job_id: str, user_id: int, envelope_from: str, recipients: list[str],
                 data: bytes) -> None:
        """Send one message to ``recipients`` (envelope only). 5xx = permanent (no retry); 4xx = limited retry."""
        pending = list(recipients)
        attempt = 0
        while pending:
            attempt += 1
            reconnect = False
            try:
                refused = transport.send(envelope_from, pending, data) or {}
            except smtplib.SMTPRecipientsRefused as exc:
                refused = exc.recipients
            except smtplib.SMTPAuthenticationError as exc:
                raise FatalSendError("SMTP authentication failed") from exc
            except SmtpSetupError as exc:
                raise FatalSendError(str(exc)) from exc
            except smtplib.SMTPResponseException as exc:  # sender refused / data refused / other reply
                refused = {r: (exc.smtp_code, exc.smtp_error) for r in pending}
                reconnect = exc.smtp_code == 421
            except (smtplib.SMTPServerDisconnected, OSError):
                refused = {r: (421, b"Connection to the SMTP server was lost") for r in pending}
                reconnect = True
            except smtplib.SMTPException as exc:
                refused = {r: (554, type(exc).__name__.encode()) for r in pending}

            accepted = [r for r in pending if r not in refused]
            self._mark(job_id, accepted, "sent", "250 Accepted", attempt)

            temporary = [r for r, (code, _) in refused.items() if 400 <= int(code) < 500]
            permanent = [r for r in refused if r not in temporary]
            for r in permanent:
                code, resp = refused[r]
                response = f"{code} {_decode(resp)}"
                self._mark(job_id, [r], "failed", response, attempt)
                if _BAD_MAILBOX.match(response):
                    suppression.add(user_id, [r], "bounced")

            if temporary and attempt <= settings.max_retries and not self._cancel.is_set():
                self._cancel.wait(settings.retry_delay)
                if reconnect:
                    try:
                        transport.open()
                    except smtplib.SMTPAuthenticationError as exc:
                        raise FatalSendError("SMTP authentication failed") from exc
                    except (smtplib.SMTPException, SmtpSetupError, OSError):
                        pass  # the next attempt fails as temporary and is counted
                pending = temporary
            else:
                for r in temporary:
                    code, resp = refused[r]
                    self._mark(job_id, [r], "failed",
                               f"{code} {_decode(resp)} (temporary failure, gave up after {attempt} attempts)", attempt)
                pending = []


engine = SendEngine()
