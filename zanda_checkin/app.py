"""Zanda Checkin service: admin portal, check-in portal and desktop-notifier API."""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .accounts import Accounts
from .db import AuditEvent, Device, make_sessionmaker
from .keyring import Keyring
from .registry import Broadcaster, ServiceRegistry
from .security import KeyBox, RateLimiter
from .settings import Settings
from .zanda import ZandaClient

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

SEEN_EVERY = timedelta(minutes=5)
MAX_BODY = 64 * 1024


class _TooLarge(Exception):
    pass


class BodyLimit:
    """Raw ASGI middleware: reject request bodies over MAX_BODY before they're parsed,
    whether the client sends Content-Length or streams a chunked upload."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
            return await self._reject(send)
        seen = 0
        started = False

        async def capped_receive():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > MAX_BODY:
                raise _TooLarge()
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, tracked_send)
        except _TooLarge:
            if not started:
                await self._reject(send)
        except BaseExceptionGroup as group:  # raised through anyio task groups
            if group.subgroup(_TooLarge) is None:
                raise
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error":"Request too large"}'})


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
        raise HTTPException(400, "This form has expired. Go back, refresh the page and try again.")


templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["app_version"] = __version__


def render(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("public_url", request.app.state.settings.public_url or str(request.base_url).rstrip("/"))
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


def kiosk_cookie_name(settings: Settings) -> str:
    return "__Secure-zc_kiosk" if settings.secure_cookies else "zc_kiosk"


def create_app(settings: Settings | None = None, client_factory=None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    if client_factory is None:
        if settings.demo:
            from .demo import DemoZandaClient
            client_factory = DemoZandaClient
        else:
            client_factory = ZandaClient

    @asynccontextmanager
    async def lifespan(app):
        # Remove expired sessions (and their key copies) now and every 5 minutes.
        def purge():
            db = app.state.Session()
            try:
                app.state.accounts.purge_expired(db)
            finally:
                db.close()

        async def loop():
            while True:
                await asyncio.sleep(300)
                await run_in_threadpool(purge)

        await run_in_threadpool(purge)
        task = asyncio.create_task(loop())
        yield
        task.cancel()

    app = FastAPI(title="Zanda Checkin", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.settings = settings
    app.state.Session = make_sessionmaker(settings.database_url)
    pepper = KeyBox(settings.encryption_key)
    app.state.pepper = pepper
    app.state.broadcaster = Broadcaster()
    app.state.registry = ServiceRegistry(app.state.broadcaster, client_factory)
    app.state.accounts = Accounts(Keyring(pepper), pepper,
                                  pair_limiter=RateLimiter(limit=10, window=15 * 60),
                                  email_limiter=RateLimiter(limit=50, window=15 * 60),
                                  ip_limiter=RateLimiter(limit=40, window=15 * 60))
    app.state.signup_limiter = RateLimiter(limit=5, window=60 * 60)
    app.state.kiosk_cookie = kiosk_cookie_name(settings)

    # Middleware runs bottom-up: the last added runs first.
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                       session_cookie="__Host-zc_session" if settings.secure_cookies else "zc_session",
                       max_age=12 * 60 * 60, same_site="lax", https_only=settings.secure_cookies)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
                                              "interest-cohort=()")
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
            + ("; upgrade-insecure-requests" if settings.secure_cookies else ""))
        if settings.secure_cookies:
            resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        return resp

    if settings.secure_cookies:
        public = urlsplit(settings.public_url)

        @app.middleware("http")
        async def https_only(request: Request, call_next):
            # Behind the TLS proxy, uvicorn's proxy headers set the scheme from X-Forwarded-Proto.
            if request.url.scheme != "https" and request.url.path != "/healthz":
                if request.method in ("GET", "HEAD"):
                    target = f"https://{public.netloc}{request.url.path}"
                    if request.url.query:
                        target += f"?{request.url.query}"
                    return RedirectResponse(target, status_code=308)
                return JSONResponse({"error": "HTTPS required"}, status_code=400)
            return await call_next(request)

        app.add_middleware(TrustedHostMiddleware,
                           allowed_hosts=[public.hostname, "localhost", "127.0.0.1"])

    app.add_middleware(BodyLimit)

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
