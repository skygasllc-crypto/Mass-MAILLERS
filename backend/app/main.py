"""FastAPI application entry point: API + static single-page frontend."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import admin, api, public
from .config import settings
from .db import init_db
from .users.store import ensure_admin
from .utils.security import rate_limit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mailer")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; frame-src 'self'; frame-ancestors 'none'; "
    "form-action 'self'; base-uri 'none'; object-src 'none'"
)


def create_app() -> FastAPI:
    init_db()
    ensure_admin()
    app = FastAPI(title="Mass Mailer", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            try:
                rate_limit(request, "api", limit=600, window=60)
            except Exception:
                return JSONResponse({"detail": "Too many requests. Please slow down."}, status_code=429)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(public)
    app.include_router(api)
    app.include_router(admin)

    @app.get("/api/health")
    def health():
        return {"ok": True, "email_mode": settings.email_mode}

    frontend = settings.frontend_dir
    app.mount("/static", StaticFiles(directory=frontend), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(frontend / "index.html")

    log.info("Mass Mailer ready - EMAIL_MODE=%s%s", settings.email_mode,
             " (emails go to the local test mailbox, nothing is really sent)" if settings.is_development else "")
    return app


app = create_app()
