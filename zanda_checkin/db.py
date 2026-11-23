"""Database models: practices (organisations), their staff accounts, sites and devices."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Organisation(Base):
    __tablename__ = "organisations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    users: Mapped[list["User"]] = relationship(back_populates="organisation", cascade="all, delete-orphan")
    sites: Mapped[list["Site"]] = relationship(back_populates="organisation", cascade="all, delete-orphan",
                                               order_by="Site.name")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    # Salt for the password-derived key that unlocks the practice's master key (see keyring.py).
    kek_salt: Mapped[str] = mapped_column(String(64), default="")
    # Authenticator-app MFA. The secret is encrypted with the server key; recovery codes are hashed.
    totp_secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last_step: Mapped[int] = mapped_column(Integer, default=0)
    recovery_code_hashes: Mapped[str] = mapped_column(Text, default="")  # newline-separated sha256
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    organisation: Mapped[Organisation] = relationship(back_populates="users")


class KeyWrap(Base):
    """The practice's master key, wrapped for one holder (a user, a kiosk device or a web session)."""
    __tablename__ = "key_wraps"
    id: Mapped[int] = mapped_column(primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    holder: Mapped[str] = mapped_column(String(80), unique=True, index=True)  # "user:1", "device:3", "session:<id>"
    wrapped: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebSession(Base):
    """Server-side record of a signed-in browser, so sessions can be revoked."""
    __tablename__ = "web_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))  # "admin" | "kiosk_setup"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Site(Base):
    """One check-in point: a Zanda account (API key) plus the location it checks people in for."""
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    welcome_name: Mapped[str] = mapped_column(String(120), default="")
    zanda_region: Mapped[str] = mapped_column(String(4), default="au")
    # Encrypted with the practice's master key, which the server can't unlock on its own.
    zanda_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    zanda_key_hint: Mapped[str] = mapped_column(String(8), default="")
    location_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location_name: Mapped[str] = mapped_column(String(120), default="")
    time_zone: Mapped[str] = mapped_column(String(64), default="Australia/Sydney")
    minutes_early: Mapped[int] = mapped_column(Integer, default=90)
    minutes_late: Mapped[int] = mapped_column(Integer, default=30)
    reception_flag: Mapped[str] = mapped_column(String(120), default="")
    # Also ask for the first letter of the surname (stronger match, one extra tap).
    require_surname_initial: Mapped[bool] = mapped_column(Boolean, default=True)
    # Show the clinician's name on the "Is this you?" screen (others in the room may see it).
    show_clinician: Mapped[bool] = mapped_column(Boolean, default=False)
    # Put the client's first name and surname initial in staff notifications (else "A client").
    notify_names: Mapped[bool] = mapped_column(Boolean, default=True)
    # False = notify-only: staff are notified but the appointment isn't changed in Zanda.
    mark_arrived: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    organisation: Mapped[Organisation] = relationship(back_populates="sites")

    @property
    def is_ready(self) -> bool:
        return bool(self.zanda_key_encrypted and self.location_id)


class Device(Base):
    """A signed-in check-in iPad ("kiosk") or desktop notifier app ("notifier").

    Only a SHA-256 hash of the device's token is stored. Revoking a device signs it out.
    """
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    site_id: Mapped[int | None] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # "kiosk" | "notifier"
    name: Mapped[str] = mapped_column(String(120), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    site: Mapped[Site | None] = relationship()
    user: Mapped[User | None] = relationship()


class AuditEvent(Base):
    """Who changed what. Never holds client details or API keys."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    organisation_id: Mapped[int] = mapped_column(ForeignKey("organisations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class CheckinLog(Base):
    """One row per kiosk attempt, for counts and troubleshooting. No client details."""
    __tablename__ = "checkin_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    outcome: Mapped[str] = mapped_column(String(24))  # checked_in | reception_none | reception_several | ...
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


def make_sessionmaker(database_url: str):
    kwargs = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, pool_pre_ping=True, **kwargs)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
