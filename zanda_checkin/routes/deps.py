"""Shared request dependencies."""
from __future__ import annotations

from fastapi import Depends, Request

from ..app import LoginRequired
from ..db import User


def get_db(request: Request):
    db = request.app.state.Session()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db=Depends(get_db)) -> User:
    uid = request.session.get("user_id")
    user = db.get(User, uid) if uid else None
    if user is None:
        request.session.pop("user_id", None)
        raise LoginRequired("/login")
    return user


def authenticate(request: Request, db, email: str, password: str) -> tuple[User | None, str | None]:
    """Shared by every sign-in form. Returns (user, error message)."""
    from datetime import datetime, timezone

    from ..app import client_ip
    from ..security import dummy_verify, verify_password

    email = (email or "").strip().lower()
    limiter, ip_limiter = request.app.state.login_limiter, request.app.state.ip_limiter
    ip = client_ip(request)
    if limiter.blocked(email) or ip_limiter.blocked(ip):
        return None, "Too many sign-in attempts. Wait 15 minutes and try again."
    user = db.query(User).filter_by(email=email).one_or_none()
    if user is None:
        dummy_verify(password or "")
    if user is None or not verify_password(password or "", user.password_hash):
        limiter.hit(email)
        ip_limiter.hit(ip)
        return None, "That email and password don't match an account."
    limiter.reset(email)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return user, None
