"""Zanda Checkin service: admin portal, check-in portal and desktop-notifier API."""
from __future__ import annotations

import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .db import AuditEvent, Device, make_sessionmaker
from .registry import Broadcaster, ServiceRegistry
from .security import KeyBox, RateLimiter, hash_token
from .settings import Settings
from .zanda import ZandaClient

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

KIOSK_COOKIE = "zc_kiosk"
SEEN_EVERY = timedelta(minutes=5)


class LoginRequired(Exception):
    def __init__(self, to: str = "/login"):
        self.to = to


templates = Jinja2Templates(directory=str(HERE / "templates"))


def csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def check_csrf(request: Request, sent: str) -> None:
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(sent or "", expected):
        from fastapi import HTTPException
        raise HTTPException(400, "This form has expired. Go back, refresh the page and try again.")


templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["app_version"] = __version__


def render(request: Request, name: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def audit(db, organisation_id: int, user_id: int | None, action: str, detail: str = "") -> None:
    db.add(AuditEvent(organisation_id=organisation_id, user_id=user_id, action=action, detail=detail[:255]))


def touch(db, device: Device) -> None:
    now = datetime.now(timezone.utc)
    last = device.last_seen_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or now - last > SEEN_EVERY:
        device.last_seen_at = now
        db.commit()


def find_device(db, token: str | None, kind: str) -> Device | None:
    if not token:
        return None
    device = db.query(Device).filter_by(token_hash=hash_token(token), kind=kind, revoked=False).one_or_none()
    return device


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_app(settings: Settings | None = None, client_factory=None) -> FastAPI:
    settings = settings or Settings.from_env()
    if client_factory is None:
        if settings.demo:
            from .demo import DemoZandaClient
            client_factory = DemoZandaClient
        else:
            client_factory = ZandaClient

    app = FastAPI(title="Zanda Checkin", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.Session = make_sessionmaker(settings.database_url)
    app.state.keybox = KeyBox(settings.encryption_key)
    app.state.broadcaster = Broadcaster()
    app.state.registry = ServiceRegistry(app.state.keybox, app.state.broadcaster, client_factory)
    app.state.login_limiter = RateLimiter(limit=10, window=15 * 60)
    app.state.ip_limiter = RateLimiter(limit=40, window=15 * 60)

    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="zc_session",
                       max_age=12 * 60 * 60, same_site="lax", https_only=settings.secure_cookies)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if settings.secure_cookies:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resp

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse(exc.to, status_code=303)

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"ok": True, "version": __version__})

    from .routes import admin, api, kiosk
    app.include_router(admin.router)
    app.include_router(kiosk.router)
    app.include_router(api.router)
    return app
