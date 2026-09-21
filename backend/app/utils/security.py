"""Login sessions, CSRF protection and simple in-memory rate limiting."""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..config import settings

SESSION_COOKIE = "mm_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class Session:
    token: str
    csrf: str
    expires: float
    user_id: int


@dataclass
class CurrentUser:
    id: int
    email: str
    role: str
    csrf: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, user_id: int) -> Session:
        session = Session(
            token=secrets.token_urlsafe(32),
            csrf=secrets.token_urlsafe(32),
            expires=time.time() + settings.session_hours * 3600,
            user_id=user_id,
        )
        with self._lock:
            self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session and session.expires < time.time():
                del self._sessions[token]
                return None
            return session

    def delete(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def delete_user(self, user_id: int) -> None:
        """Sign a user out everywhere (after disabling or deleting the account)."""
        with self._lock:
            for token in [t for t, s in self._sessions.items() if s.user_id == user_id]:
                del self._sessions[token]


class RateLimiter:
    """Sliding-window limiter keyed by (bucket, client IP)."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, bucket: str, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[(bucket, key)]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


sessions = SessionStore()
limiter = RateLimiter()


def client_ip(request: Request) -> str:
    """The visitor's IP address, used for rate limiting.

    Behind a reverse proxy (e.g. Render) every request comes from the proxy, so the real address is taken
    from X-Forwarded-For. Only the entry added by our own proxy is used: counting ``trusted_proxy_hops``
    from the right. Entries further left are supplied by the client and could be faked.
    """
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, bucket: str, limit: int, window: float = 60.0) -> None:
    if not limiter.check(bucket, client_ip(request), limit, window):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment and try again.")


def current_user(request: Request) -> CurrentUser | None:
    """The signed-in, still-active user for this request (None if not signed in)."""
    from ..users.store import get as get_user

    token = request.cookies.get(SESSION_COOKIE)
    session = sessions.get(token)
    if session is None:
        return None
    user = get_user(session.user_id)
    if user is None or user["status"] != "active":  # disabled or deleted since signing in
        sessions.delete(token)
        return None
    return CurrentUser(id=user["id"], email=user["email"], role=user["role"], csrf=session.csrf)


def require_session(request: Request) -> CurrentUser:
    """FastAPI dependency: valid login session, plus CSRF token on state-changing requests."""
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Please sign in.")
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent.encode(), user.csrf.encode()):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page.")
    return user


def require_admin(request: Request) -> CurrentUser:
    user = require_session(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user
