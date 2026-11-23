"""Shared request dependencies."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from ..app import LoginRequired
from ..db import User, WebSession


def get_db(request: Request):
    db = request.app.state.Session()
    try:
        yield db
    finally:
        db.close()


@dataclass
class Admin:
    user: User
    session: WebSession

    @property
    def org_id(self) -> int:
        return self.user.organisation_id


def _admin(request: Request, db) -> Admin:
    ws = request.app.state.accounts.session(request, db, "admin")
    user = db.get(User, ws.user_id) if ws else None
    if user is None:
        request.session.clear()
        raise LoginRequired("/login")
    return Admin(user, ws)


def signed_in(request: Request, db=Depends(get_db)) -> Admin:
    """Signed in, MFA not necessarily set up yet (only for the MFA set-up pages)."""
    return _admin(request, db)


def current_admin(request: Request, db=Depends(get_db)) -> Admin:
    """Signed in with two-step sign-in set up. Everything in the admin portal needs this."""
    admin = _admin(request, db)
    if not admin.user.totp_enabled:
        raise LoginRequired("/admin/mfa")
    return admin


def org_key(request: Request, admin: Admin, db) -> bytes:
    """The practice master key, unlocked with this browser's session secret."""
    omk = request.app.state.accounts.session_omk(request, db, admin.session)
    if omk is None:
        request.session.clear()
        raise LoginRequired("/login")
    return omk
