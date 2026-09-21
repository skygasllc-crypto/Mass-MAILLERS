"""Per-user do-not-send list: unsubscribed addresses and addresses that bounced permanently.

Addresses on a user's list are skipped in every future send by that user.
"""
from __future__ import annotations

from ..db import get_db, now_iso
from .parser import Recipient, normalize_email

REASONS = {"unsubscribed", "bounced", "manual"}


def add(user_id: int, emails: list[str], reason: str = "manual") -> int:
    reason = reason if reason in REASONS else "manual"
    with get_db() as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO suppressions(user_id, email, reason, created_at) VALUES(?,?,?,?)",
            [(user_id, normalize_email(e), reason, now_iso()) for e in emails if e.strip()],
        )
        return conn.total_changes - before


def remove(user_id: int, emails: list[str]) -> int:
    with get_db() as conn:
        before = conn.total_changes
        conn.executemany("DELETE FROM suppressions WHERE user_id=? AND email=?",
                         [(user_id, normalize_email(e)) for e in emails])
        return conn.total_changes - before


def list_all(user_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT email, reason, created_at FROM suppressions WHERE user_id=? "
                            "ORDER BY created_at DESC, email", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def all_emails(user_id: int) -> set[str]:
    with get_db() as conn:
        return {r["email"] for r in conn.execute("SELECT email FROM suppressions WHERE user_id=?", (user_id,))}


def split(user_id: int, recipients: list[Recipient]) -> tuple[list[Recipient], list[str]]:
    """Return (recipients to send to, suppressed addresses)."""
    blocked = all_emails(user_id)
    if not blocked:
        return recipients, []
    keep = [r for r in recipients if r.email not in blocked]
    return keep, [r.email for r in recipients if r.email in blocked]
