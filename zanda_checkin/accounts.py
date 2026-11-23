"""Sign-in, sessions and the practice master key, in one place.

Every route that needs a practice's Zanda key gets it through here, from something only the
practice holds: a password at sign-in, a browser session secret, or an iPad's device token.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from sqlalchemy import update
from datetime import datetime, timedelta, timezone

from fastapi import Request

from . import mfa
from .db import Device, KeyWrap, Organisation, Site, User, WebSession
from .keyring import Keyring, kek_from_password, kek_from_secret, new_omk, new_salt
from .security import dummy_verify, hash_password, hash_token, verify_password

ADMIN_SESSION_HOURS = 12
SETUP_SESSION_MINUTES = 5


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class SignIn:
    user: User | None = None
    omk: bytes | None = None
    error: str | None = None
    needs_code: bool = False


class Accounts:
    def __init__(self, keyring: Keyring, pepper, pair_limiter, email_limiter, ip_limiter):
        self.keyring = keyring
        self.pepper = pepper  # KeyBox with the server key, for the TOTP secret
        # Three limits: a tight one per email+IP (stops guessing), a looser one per email
        # (stops spread-out guessing without letting one IP lock a person out), and one per IP.
        self.pair_limiter = pair_limiter
        self.email_limiter = email_limiter
        self.ip_limiter = ip_limiter

    # -- creating a practice ------------------------------------------------------
    def create_practice(self, db, practice_name: str, name: str, email: str, password: str) -> User:
        org = Organisation(name=practice_name)
        user = User(organisation=org, email=email, name=name, password_hash=hash_password(password),
                    kek_salt=new_salt())
        db.add_all([org, user])
        db.flush()
        omk = new_omk()
        self._store_wrap(db, org.id, f"user:{user.id}", omk, kek_from_password(password, user.kek_salt))
        return user

    def _store_wrap(self, db, org_id: int, holder: str, omk: bytes, kek: bytes, bind: str = "") -> None:
        """`bind` is extra authenticated data (e.g. a session's expiry) the wrap is only valid with."""
        db.query(KeyWrap).filter_by(holder=holder).delete()
        db.add(KeyWrap(organisation_id=org_id, holder=holder,
                       wrapped=self.keyring.wrap_omk(omk, kek, org_id, holder + bind)))

    def _unwrap(self, db, org_id: int, holder: str, kek: bytes, bind: str = "") -> bytes | None:
        wrap = db.query(KeyWrap).filter_by(holder=holder, organisation_id=org_id).one_or_none()
        if wrap is None:
            return None
        try:
            return self.keyring.unwrap_omk(wrap.wrapped, kek, org_id, holder + bind)
        except ValueError:
            return None

    # -- signing in -------------------------------------------------------------
    def authenticate(self, request: Request, db, email: str, password: str, code: str = "",
                     require_mfa: bool = True, scope: str = "login") -> SignIn:
        """Password, then authenticator code (or a recovery code). Unlocks the master key on success.

        `scope` keeps rate limits separate: failed attempts on a waiting-room iPad's "sign out"
        screen never count against normal sign-in, so nobody at the iPad can lock staff out.
        """
        email = (email or "").strip().lower()[:254]
        ip = request.client.host if request.client else "unknown"
        keys = (f"{scope}:{email}:{ip}", f"{scope}:{email}", f"{scope}:{ip}")
        limiters = (self.pair_limiter, self.email_limiter, self.ip_limiter)
        if any(lim.blocked(k) for lim, k in zip(limiters, keys)):
            return SignIn(error="Too many sign-in attempts. Wait 15 minutes and try again.")

        def fail():
            for lim, k in zip(limiters, keys):
                lim.hit(k)

        user = db.query(User).filter_by(email=email).one_or_none()
        if user is None:
            dummy_verify(password or "")
        if user is None or not verify_password(password or "", user.password_hash):
            fail()
            return SignIn(error="That email and password don't match an account.")
        if user.totp_enabled:
            if not (code or "").strip():
                return SignIn(error="Enter the 6-digit code from your authenticator app.", needs_code=True)
            if not self.check_code(db, user, code):
                fail()
                return SignIn(error="That code didn't work. Check your authenticator app and try again.",
                              needs_code=True)
        elif require_mfa:
            return SignIn(error="Set up two-step sign-in in the admin portal first (Account › Two-step sign-in).")
        omk = self._unwrap(db, user.organisation_id, f"user:{user.id}",
                           kek_from_password(password, user.kek_salt))
        if omk is None:
            return SignIn(error="Your account's encryption key couldn't be unlocked. Contact support.")
        self.pair_limiter.reset(keys[0])
        user.last_login_at = utcnow()
        db.commit()
        return SignIn(user=user, omk=omk)

    def check_code(self, db, user: User, code: str) -> bool:
        """Accept each code once only, even under simultaneous requests (compare-and-set update)."""
        secret = self.pepper.decrypt(user.totp_secret_encrypted)
        step = mfa.verify(secret, code, user.totp_last_step)
        if step is not None:
            done = db.execute(update(User).where(User.id == user.id, User.totp_last_step < step)
                              .values(totp_last_step=step))
            db.commit()
            db.refresh(user)
            return done.rowcount == 1
        before = user.recovery_code_hashes
        remaining = mfa.use_recovery_code(before, code)
        if remaining is not None:
            done = db.execute(update(User).where(User.id == user.id, User.recovery_code_hashes == before)
                              .values(recovery_code_hashes=remaining))
            db.commit()
            db.refresh(user)
            return done.rowcount == 1
        return False

    # -- browser sessions -------------------------------------------------------
    def start_session(self, request: Request, db, user: User, omk: bytes, purpose: str = "admin") -> None:
        sid, secret = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.purge_expired(db)
        ttl = timedelta(hours=ADMIN_SESSION_HOURS) if purpose == "admin" else timedelta(minutes=SETUP_SESSION_MINUTES)
        expires = (utcnow() + ttl).replace(microsecond=0)
        db.add(WebSession(id=sid, user_id=user.id, organisation_id=user.organisation_id,
                          purpose=purpose, expires_at=expires))
        # The key copy is cryptographically tied to the expiry, so it can't be used after it.
        self._store_wrap(db, user.organisation_id, f"session:{sid}", omk, kek_from_secret(secret, "session"),
                         bind=f"@{int(expires.timestamp())}")
        db.commit()
        csrf = request.session.get("csrf")
        request.session.clear()
        request.session.update({"sid": sid, "sk": secret, "purpose": purpose})
        if csrf:
            request.session["csrf"] = csrf

    def session(self, request: Request, db, purpose: str = "admin") -> WebSession | None:
        sid = request.session.get("sid")
        if not sid or request.session.get("purpose") != purpose:
            return None
        ws = db.get(WebSession, sid)
        if ws is None or ws.purpose != purpose:
            return None
        if aware(ws.expires_at) < utcnow():
            self._drop_session(db, ws.id)  # delete the expired session's key copy straight away
            db.commit()
            return None
        return ws

    def session_omk(self, request: Request, db, ws: WebSession) -> bytes | None:
        if aware(ws.expires_at) < utcnow():
            return None
        secret = request.session.get("sk", "")
        return self._unwrap(db, ws.organisation_id, f"session:{ws.id}", kek_from_secret(secret, "session"),
                            bind=f"@{int(aware(ws.expires_at).timestamp())}")

    def end_session(self, request: Request, db) -> None:
        sid = request.session.get("sid")
        if sid:
            self._drop_session(db, sid)
            db.commit()
        request.session.clear()

    def _drop_session(self, db, sid: str) -> None:
        db.query(KeyWrap).filter_by(holder=f"session:{sid}").delete()
        db.query(WebSession).filter_by(id=sid).delete()

    def end_all_sessions(self, db, user: User, except_sid: str | None = None) -> None:
        for ws in db.query(WebSession).filter_by(user_id=user.id).all():
            if ws.id != except_sid:
                self._drop_session(db, ws.id)

    def purge_expired(self, db) -> int:
        """Delete expired sessions and their key copies. Runs at start-up, on every sign-in and every few minutes."""
        expired = [ws.id for ws in db.query(WebSession).all() if aware(ws.expires_at) < utcnow()]
        for sid in expired:
            self._drop_session(db, sid)
        # Belt and braces: remove any session key copy whose session no longer exists.
        live = {f"session:{sid}" for (sid,) in db.query(WebSession.id).all()}
        for wrap in db.query(KeyWrap).filter(KeyWrap.holder.like("session:%")).all():
            if wrap.holder not in live:
                db.delete(wrap)
        db.commit()
        return len(expired)

    # -- devices ----------------------------------------------------------------
    def wrap_for_device(self, db, device: Device, token: str, omk: bytes) -> None:
        self._store_wrap(db, device.organisation_id, f"device:{device.id}", omk, kek_from_secret(token, "device"))

    def device_omk(self, db, device: Device, token: str) -> bytes | None:
        return self._unwrap(db, device.organisation_id, f"device:{device.id}", kek_from_secret(token, "device"))

    def forget_device(self, db, device: Device) -> None:
        device.revoked = True
        db.query(KeyWrap).filter_by(holder=f"device:{device.id}").delete()

    # -- passwords --------------------------------------------------------------
    def change_password(self, db, user: User, old: str, new: str, keep_sid: str | None) -> bool:
        omk = self._unwrap(db, user.organisation_id, f"user:{user.id}", kek_from_password(old, user.kek_salt))
        if omk is None:
            return False
        user.password_hash = hash_password(new)
        user.kek_salt = new_salt()
        self._store_wrap(db, user.organisation_id, f"user:{user.id}", omk, kek_from_password(new, user.kek_salt))
        self.end_all_sessions(db, user, except_sid=keep_sid)
        db.commit()
        return True

    # -- Zanda keys ---------------------------------------------------------------
    def seal_api_key(self, omk: bytes, site: Site, api_key: str) -> None:
        site.zanda_key_encrypted = self.keyring.encrypt_api_key(omk, api_key, site.organisation_id, site.id)
        site.zanda_key_hint = api_key[-4:]

    def open_api_key(self, omk: bytes, site: Site) -> str:
        return self.keyring.decrypt_api_key(omk, site.zanda_key_encrypted, site.organisation_id, site.id)


def device_by_token(db, token: str | None, kind: str) -> Device | None:
    if not token:
        return None
    return db.query(Device).filter_by(token_hash=hash_token(token), kind=kind, revoked=False).one_or_none()
