"""JSON API.

Public: login, register, session. Everything else needs a signed-in, active account (+ CSRF on writes),
and only ever touches that account's own data. /api/admin/* additionally requires the admin role.
"""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse

from ..attachments import store as attachment_store
from ..config import CONSERVATIVE_DELAY_CHOICES, settings
from ..db import get_db
from ..email_templates import store as template_store
from ..models.schemas import (
    LoginIn, PasswordChangeIn, PreviewIn, RecipientsIn, RegisterIn, SendIn, SmtpProfileIn, SmtpSettingsIn, SuppressionIn,
    TemplateIn, TestSendIn, UserStatusIn,
)
from ..recipients import suppression
from ..recipients.parser import ParseResult, is_valid_email, parse_csv, parse_text, to_text
from ..sending.engine import (
    SendRequest, active_job_ids, attachments_in_use, create_job, engine, job_progress, job_results, latest_job_id,
    prepare, send_test,
)
from ..sending.message import Compose, MessageError, build_message, message_bytes
from ..smtp import config_store
from ..smtp.client import test_connection
from ..smtp.deliverability import check as deliverability_check
from ..users import store as user_store
from ..utils.security import (
    SESSION_COOKIE, CurrentUser, current_user, limiter, rate_limit, require_admin, require_session, sessions,
)

public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api", dependencies=[Depends(require_session)])
admin = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

MAX_INVALID_SHOWN = 200
User = CurrentUser  # annotation shorthand


# ------------------------------------------------------------------ auth

def _start_session(response: Response, user_id: int) -> dict:
    session = sessions.create(user_id)
    response.set_cookie(
        SESSION_COOKIE, session.token, httponly=True, samesite="strict",
        secure=settings.cookie_secure, max_age=settings.session_hours * 3600, path="/",
    )
    return {"ok": True, "csrf": session.csrf}


@public.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    rate_limit(request, "login", limit=10, window=300)
    if not limiter.check("login-email", body.email.strip().lower(), 10, 300):  # per account, whatever the IP
        raise HTTPException(status_code=429, detail="Too many sign-in attempts for this account. Try again in 5 minutes.")
    try:
        user = user_store.authenticate(body.email, body.password)
    except user_store.UserError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return _start_session(response, user["id"])


@public.post("/auth/register")
def register(body: RegisterIn, request: Request, response: Response):
    rate_limit(request, "register", limit=5, window=3600)
    try:
        user = user_store.register(body.email, body.password)
    except user_store.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if user["status"] != "active":
        return {"ok": True, "pending": True,
                "message": "Account created. You can sign in once the administrator approves it."}
    return {**_start_session(response, user["id"]), "pending": False}


@public.get("/auth/session")
def session_info(request: Request):
    user = current_user(request)
    return {
        "authenticated": user is not None,
        "csrf": user.csrf if user else None,
        "user": {"id": user.id, "email": user.email, "role": user.role} if user else None,
        "email_mode": settings.email_mode,
        "registration": settings.allow_registration,
    }


@api.post("/auth/logout")
def logout(request: Request, response: Response):
    sessions.delete(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@api.post("/auth/password")
def change_password(body: PasswordChangeIn, user: User = Depends(require_session)):
    try:
        user_store.change_password(user.id, body.current_password, body.new_password)
    except user_store.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True}


# ------------------------------------------------------------------ admin: users

@admin.get("/users")
def admin_users():
    return user_store.list_users()


@admin.post("/users/{user_id}/status")
def admin_set_status(user_id: int, body: UserStatusIn, user: User = Depends(require_admin)):
    try:
        updated = user_store.set_status(user.id, user_id, body.status)
    except user_store.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if updated["status"] != "active":
        sessions.delete_user(user_id)
    return user_store.public(updated)


@admin.delete("/users/{user_id}")
def admin_delete_user(user_id: int, user: User = Depends(require_admin)):
    if engine.busy(user_id):
        raise HTTPException(status_code=409, detail="This user is sending right now. Disable the account "
                                                    "first, then delete it when the send has finished.")
    try:
        user_store.delete(user.id, user_id)
    except user_store.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    sessions.delete_user(user_id)
    return {"ok": True}


# ------------------------------------------------------------------ config

@api.get("/config")
def app_config(user: User = Depends(require_session)):
    return {
        "email_mode": settings.email_mode,
        "user": {"id": user.id, "email": user.email, "role": user.role,
                 "main_admin": user.email == settings.admin_email},
        "max_recipients": settings.max_recipients,
        "max_parallel_sends": settings.max_parallel_sends,
        "default_batch_size": settings.default_batch_size,
        "max_batch_size": settings.max_batch_size,
        "max_attachment_mb": settings.max_attachment_mb,
        "max_message_mb": settings.max_message_mb,
        "allowed_extensions": sorted(settings.allowed_extensions),
        "speeds": settings.speed_delays,
        "conservative_delays": list(CONSERVATIVE_DELAY_CHOICES),
        "message_delays": settings.message_delays,
    }


# ------------------------------------------------------------------ SMTP

@api.get("/smtp")
def get_smtp(user: User = Depends(require_session)):
    return config_store.load(user.id).public()


@api.put("/smtp")
def save_smtp(body: SmtpSettingsIn, user: User = Depends(require_session)):
    cfg = config_store.SmtpConfig(
        host=body.host.strip(), port=body.port, security=body.security, username=body.username.strip(),
        from_name=" ".join(body.from_name.split()), from_email=body.from_email.strip().lower(),
        reply_to=body.reply_to.strip().lower(), to_address=body.to_address.strip().lower(),
        unsubscribe_email=body.unsubscribe_email.strip().lower(), unsubscribe_url=body.unsubscribe_url.strip(),
        company_address=body.company_address.strip(), add_footer=body.add_footer,
    )
    for value in (cfg.host, cfg.username, cfg.from_name, cfg.from_email, cfg.reply_to, cfg.to_address,
                  cfg.unsubscribe_email, cfg.unsubscribe_url):
        if "\r" in value or "\n" in value:
            raise HTTPException(status_code=422, detail="Fields cannot contain line breaks.")
    if cfg.unsubscribe_email and not is_valid_email(cfg.unsubscribe_email):
        raise HTTPException(status_code=422, detail="The unsubscribe email address is not valid.")
    if cfg.unsubscribe_url and (not cfg.unsubscribe_url.lower().startswith("https://")
                                or not cfg.unsubscribe_url.isascii()
                                or any(c in cfg.unsubscribe_url for c in ' <>"')):
        raise HTTPException(status_code=422, detail="The unsubscribe link must be a full https:// address.")
    password = body.password if body.password else None  # blank = keep existing
    config_store.save(user.id, cfg, password)
    return config_store.load(user.id).public()


@api.post("/smtp/test")
def smtp_test(request: Request, user: User = Depends(require_session)):
    rate_limit(request, "smtp-test", limit=10, window=60)
    cfg = config_store.load(user.id)
    result = test_connection(cfg)
    if result["ok"]:
        config_store.mark_verified(cfg)
        config_store.mark_profiles_verified(cfg)
    result["settings"] = config_store.load(user.id).public()
    return result


@api.get("/smtp/profiles")
def smtp_profiles(user: User = Depends(require_session)):
    return config_store.list_profiles(user.id)


@api.post("/smtp/profiles")
def smtp_profile_save(body: SmtpProfileIn, user: User = Depends(require_session)):
    """Save the current SMTP settings (as last saved with PUT /smtp) under a name."""
    try:
        return config_store.save_profile(user.id, body.name)
    except config_store.ProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@api.post("/smtp/profiles/{profile_id}/use")
def smtp_profile_use(profile_id: int, user: User = Depends(require_session)):
    try:
        return config_store.use_profile(user.id, profile_id).public()
    except config_store.ProfileError as exc:
        raise HTTPException(status_code=404 if "not found" in str(exc) else 422, detail=str(exc))


@api.delete("/smtp/profiles/{profile_id}")
def smtp_profile_delete(profile_id: int, user: User = Depends(require_session)):
    if not config_store.delete_profile(user.id, profile_id):
        raise HTTPException(status_code=404, detail="Saved SMTP account not found.")
    return {"ok": True}


@api.post("/smtp/deliverability")
def smtp_deliverability(request: Request, user: User = Depends(require_session)):
    rate_limit(request, "smtp-test", limit=10, window=60)
    return deliverability_check(config_store.load(user.id))


# ------------------------------------------------------------------ recipients

def _parse_response(user_id: int, result: ParseResult, include_text: bool = False) -> dict:
    ready, suppressed = suppression.split(user_id, result.valid)
    stats = {**result.stats(), "suppressed": len(suppressed), "ready": len(ready)}
    data = {
        "stats": stats,
        "over_limit": len(ready) > settings.max_recipients,
        "max_recipients": settings.max_recipients,
        "invalid": result.invalid[:MAX_INVALID_SHOWN],
        "duplicates": sorted(set(result.duplicates))[:MAX_INVALID_SHOWN],
        "suppressed": suppressed[:MAX_INVALID_SHOWN],
        "clean_text": to_text(ready),
    }
    if include_text:
        # Valid (de-duplicated) first, then invalid entries so the user can see and fix them.
        data["text"] = "\n".join(filter(None, [to_text(result.valid), "\n".join(result.invalid)]))
    return data


@api.post("/recipients/parse")
def recipients_parse(body: RecipientsIn, user: User = Depends(require_session)):
    return _parse_response(user.id, parse_text(body.text))


@api.post("/recipients/import")
async def recipients_import(request: Request, file: UploadFile = File(...), user: User = Depends(require_session)):
    rate_limit(request, "upload", limit=30, window=60)
    data = await file.read(2 * 1024 * 1024 + 1)
    try:
        result = parse_csv(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _parse_response(user.id, result, include_text=True)


# ------------------------------------------------------------------ attachments

@api.get("/attachments")
def attachments_list(user: User = Depends(require_session)):
    return [a.public() for a in attachment_store.list_all(user.id)]


@api.post("/attachments")
async def attachments_upload(request: Request, file: UploadFile = File(...), user: User = Depends(require_session)):
    rate_limit(request, "upload", limit=30, window=60)
    limit = int(settings.max_attachment_mb * 1024 * 1024)
    data = await file.read(limit + 1)
    try:
        att = attachment_store.save(user.id, file.filename or "attachment", data)
    except attachment_store.AttachmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return att.public()


@api.delete("/attachments/{att_id}")
def attachments_delete(att_id: str, user: User = Depends(require_session)):
    if att_id in attachments_in_use(user.id):
        raise HTTPException(status_code=409, detail="This attachment is used by a send that is still running.")
    if not attachment_store.delete(user.id, att_id):
        raise HTTPException(status_code=404, detail="Attachment not found.")
    return {"ok": True}


# ------------------------------------------------------------------ compose preview & sending

def _compose(body) -> Compose:
    c = body.compose
    return Compose(subject=c.subject, body=c.body, format=c.format,
                   personalize=c.personalize, name_fallback=c.name_fallback.strip() or "there",
                   individual=getattr(body, "delivery", "bcc") == "individual")


def _send_request(user_id: int, body: SendIn) -> SendRequest:
    recipients, _suppressed = suppression.split(user_id, parse_text(body.recipients_text).valid)
    return SendRequest(
        user_id=user_id, compose=_compose(body), recipients=recipients,
        batch_size=body.batch_size, speed=body.speed, attachment_ids=body.attachment_ids,
        conservative_delay=body.conservative_delay if body.speed == "conservative" else None,
    )


@api.post("/preview")
def preview(body: PreviewIn, user: User = Depends(require_session)):
    """Render the message exactly as it will be built (first named recipient used for {{name}})."""
    compose = _compose(body)
    cfg = config_store.load(user.id)
    sample = next((r.name for r in parse_text(body.recipients_text).valid if r.name), None)
    try:
        attachments = [(a, a.read()) for a in attachment_store.get_many(user.id, body.attachment_ids)]
        msg = build_message(cfg, compose, attachments, name=sample)
    except (attachment_store.AttachmentError, MessageError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    return {
        "subject": str(msg["Subject"] or ""),
        "html": html_part.get_content() if html_part else None,
        "text": text_part.get_content() if text_part else "",
        "size": f"{len(message_bytes(msg)) / 1024 / 1024:.2f} MB",
        "sample_name": (sample or compose.name_fallback) if compose.personalize else None,
    }


@api.post("/send/prepare")
def send_prepare(body: SendIn, user: User = Depends(require_session)):
    return prepare(_send_request(user.id, body))


@api.post("/send/test")
def send_test_route(body: TestSendIn, request: Request, user: User = Depends(require_session)):
    rate_limit(request, "send", limit=10, window=60)
    return send_test(user.id, _compose(body), body.attachment_ids, body.test_email)


@api.post("/send")
def send_start(body: SendIn, request: Request, user: User = Depends(require_session)):
    rate_limit(request, "send", limit=10, window=60)
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Sending must be confirmed.")
    account_key = config_store.load(user.id).account_key
    try:
        engine.check_can_start(user.id, account_key)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    req = _send_request(user.id, body)
    summary = prepare(req)
    if not summary["ok"]:
        raise HTTPException(status_code=422, detail=summary["errors"][0])
    job_id = create_job(req)
    try:
        engine.start(job_id, user.id, account_key)
    except RuntimeError as exc:  # another send grabbed the account in the meantime
        engine.discard(job_id, str(exc))
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "job_id": job_id}


# ------------------------------------------------------------------ templates

@api.get("/templates")
def templates_list(user: User = Depends(require_session)):
    return template_store.list_templates(user.id)


@api.post("/templates")
def templates_save(body: TemplateIn, user: User = Depends(require_session)):
    compose = body.compose.model_dump()
    try:
        data = template_store.save(user.id, body.name, compose, body.attachment_ids, body.template_id)
    except (template_store.TemplateError, attachment_store.AttachmentError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"id": data["id"], "name": data["name"]}


@api.post("/templates/{template_id}/use")
def templates_use(template_id: str, user: User = Depends(require_session)):
    try:
        return template_store.use(user.id, template_id)
    except (template_store.TemplateError, attachment_store.AttachmentError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, template_store.TemplateError) else 422,
                            detail=str(exc))


@api.delete("/templates/{template_id}")
def templates_delete(template_id: str, user: User = Depends(require_session)):
    try:
        template_store.delete(user.id, template_id)
    except template_store.TemplateError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True}


# ------------------------------------------------------------------ do-not-send list

@api.get("/suppression")
def suppression_list(user: User = Depends(require_session)):
    return suppression.list_all(user.id)


@api.post("/suppression")
def suppression_add(body: SuppressionIn, user: User = Depends(require_session)):
    result = parse_text(body.text)
    added = suppression.add(user.id, [r.email for r in result.valid], body.reason)
    return {"added": added, "invalid": result.invalid[:MAX_INVALID_SHOWN]}


@api.post("/suppression/remove")
def suppression_remove(body: SuppressionIn, user: User = Depends(require_session)):
    return {"removed": suppression.remove(user.id, [r.email for r in parse_text(body.text).valid])}


# ------------------------------------------------------------------ jobs / results

def _job_or_404(job_id: str, user_id: int) -> dict:
    progress = job_progress(job_id, user_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return progress


@api.get("/jobs/latest")
def jobs_latest(user: User = Depends(require_session)):
    job_id = latest_job_id(user.id)
    return job_progress(job_id) if job_id else None


@api.get("/jobs/active")
def jobs_active(user: User = Depends(require_session)):
    """Sends still running for this user (they keep running after logout)."""
    return [p for p in (job_progress(j) for j in active_job_ids(user.id)) if p]


@api.get("/jobs/{job_id}")
def jobs_get(job_id: str, user: User = Depends(require_session)):
    return _job_or_404(job_id, user.id)


@api.post("/jobs/{job_id}/cancel")
def jobs_cancel(job_id: str, user: User = Depends(require_session)):
    _job_or_404(job_id, user.id)
    return {"ok": engine.cancel(job_id, user.id)}


@api.get("/jobs/{job_id}/results")
def jobs_results(job_id: str, status: str | None = None, user: User = Depends(require_session)):
    _job_or_404(job_id, user.id)
    return job_results(job_id, status)


def _csv_cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text  # spreadsheet formula injection


@api.get("/jobs/{job_id}/export")
def jobs_export(job_id: str, status: str | None = None, user: User = Depends(require_session)):
    _job_or_404(job_id, user.id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "name", "status", "smtp_response", "attempts", "batch", "timestamp"])
    for r in job_results(job_id, status):
        writer.writerow([_csv_cell(v) for v in (r["email"], r["name"], r["status"].upper(), r["smtp_response"],
                                                 r["attempts"], r["batch_no"], r["updated_at"])])
    suffix = "failed" if status == "failed" else "results"
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="send-{job_id}-{suffix}.csv"'},
    )


# ------------------------------------------------------------------ development mailbox

def _dev_only():
    if not settings.is_development:
        raise HTTPException(status_code=404, detail="The test mailbox is only available in development mode.")


def _dev_row(user_id: int, msg_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM dev_outbox WHERE id=? AND user_id=?", (msg_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    return row


@api.get("/dev/outbox")
def dev_outbox(user: User = Depends(require_session)):
    _dev_only()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, envelope_from, envelope_to, subject, length(raw) AS size "
            "FROM dev_outbox WHERE user_id=? ORDER BY id DESC LIMIT 200", (user.id,)).fetchall()
    return [{**dict(r), "envelope_to": json.loads(r["envelope_to"])} for r in rows]


@api.get("/dev/outbox/{msg_id}")
def dev_outbox_message(msg_id: int, user: User = Depends(require_session)):
    _dev_only()
    import email
    from email import policy

    row = _dev_row(user.id, msg_id)
    msg = email.message_from_string(row["raw"], policy=policy.default)
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "envelope_from": row["envelope_from"],
        "envelope_to": json.loads(row["envelope_to"]),
        "headers": [[k, str(v)] for k, v in msg.items()],
        "html": html_part.get_content() if html_part else None,
        "text": text_part.get_content() if text_part else None,
        "attachments": [
            {"filename": p.get_filename(), "content_type": p.get_content_type(),
             "size": len(p.get_payload(decode=True) or b"")}
            for p in msg.iter_attachments()
        ],
        "structure": _structure(msg),
    }


def _structure(part, depth: int = 0) -> str:
    line = "  " * depth + part.get_content_type()
    if part.get_filename():
        line += f"  ({part.get_filename()})"
    lines = [line]
    if part.is_multipart():
        for sub in part.iter_parts():
            lines.append(_structure(sub, depth + 1))
    return "\n".join(lines)


@api.get("/dev/outbox/{msg_id}/raw", response_class=PlainTextResponse)
def dev_outbox_raw(msg_id: int, user: User = Depends(require_session)):
    _dev_only()
    row = _dev_row(user.id, msg_id)
    return PlainTextResponse(row["raw"], headers={"Content-Disposition": f'attachment; filename="message-{msg_id}.eml"'})


@api.delete("/dev/outbox")
def dev_outbox_clear(user: User = Depends(require_session)):
    _dev_only()
    with get_db() as conn:
        conn.execute("DELETE FROM dev_outbox WHERE user_id=?", (user.id,))
    return {"ok": True}
