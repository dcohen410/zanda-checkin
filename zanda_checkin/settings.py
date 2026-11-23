"""Service settings, all from environment variables (12-factor, container friendly).

DATA_DIR          Where SQLite and auto-generated keys live (default ./data). Mount a volume here.
DATABASE_URL      SQLAlchemy URL. Default: SQLite in DATA_DIR. Postgres: postgresql+psycopg://user:pass@host/db
SECRET_KEY        Signs session cookies. Required in production; auto-generated only for local testing.
ENCRYPTION_KEY    Fernet key: an extra encryption layer over all stored key material. Required in production
                  (keep it in a secret store, away from the database and its backups). Only auto-generated
                  into DATA_DIR for local testing (ALLOW_INSECURE_HTTP=1).
SIGNUP_CODE       New practices must enter this code to sign up (required in production unless
                  ALLOW_OPEN_SIGNUP=1).
PUBLIC_URL        The service's public https URL (shown to users; used for secure cookies when https).
ZANDA_CHECKIN_DEMO=1  Use pretend Zanda data for every site (for demos and tests).
ALLOW_INSECURE_HTTP=1 Allow plain http (local testing only). Without it the app refuses to start unless
                  PUBLIC_URL is https://.
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

log = logging.getLogger(__name__)


def _persisted_secret(data_dir: Path, name: str, make) -> str:
    path = data_dir / name
    if path.exists():
        return path.read_text().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    value = make()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)
    log.warning("Generated %s in %s. Back it up, or set it as an environment variable.", name, data_dir)
    return value


@dataclass
class Settings:
    data_dir: Path
    database_url: str
    secret_key: str
    encryption_key: str
    signup_code: str
    public_url: str
    demo: bool
    allow_insecure_http: bool = False
    keys_from_env: bool = True
    allow_open_signup: bool = False

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    def validate(self) -> None:
        """Refuse to start in an insecure configuration."""
        if not self.secure_cookies and not self.allow_insecure_http:
            raise RuntimeError(
                "PUBLIC_URL must be your https:// address (for example https://checkin.example.com). "
                "For local testing only, set ALLOW_INSECURE_HTTP=1.")
        if not self.allow_insecure_http:
            if not self.keys_from_env:
                raise RuntimeError(
                    "Set SECRET_KEY and ENCRYPTION_KEY as environment variables (from your platform's secret "
                    "store). They must not live on the same volume as the database or its backups.")
            if not self.signup_code and not self.allow_open_signup:
                raise RuntimeError("Set SIGNUP_CODE so only practices you invite can sign up "
                                   "(or ALLOW_OPEN_SIGNUP=1 to deliberately allow anyone).")
        if self.database_url.startswith("postgres"):
            parts = urlsplit(self.database_url.replace("+psycopg", ""))
            local_socket = not parts.hostname or parts.hostname.startswith("/")
            modes = dict(q.split("=", 1) for q in parts.query.split("&") if "=" in q)
            if not local_socket and modes.get("sslmode") not in ("verify-full", "verify-ca"):
                raise RuntimeError("Add ?sslmode=verify-full to DATABASE_URL: the database connection must be "
                                   "encrypted and the server's certificate checked.")

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        data_dir = Path(overrides.pop("data_dir", None) or os.environ.get("DATA_DIR", "data")).resolve()
        local_only = os.environ.get("ALLOW_INSECURE_HTTP") == "1" or "secret_key" in overrides

        def secret(env: str, make) -> str:
            # Only ever written to disk for local testing; production must supply them.
            if os.environ.get(env):
                return os.environ[env]
            return _persisted_secret(data_dir, env.lower(), make) if local_only else ""

        values = dict(
            data_dir=data_dir,
            database_url=os.environ.get("DATABASE_URL") or f"sqlite:///{data_dir / 'zanda-checkin.db'}",
            secret_key=secret("SECRET_KEY", lambda: secrets.token_hex(32)),
            encryption_key=secret("ENCRYPTION_KEY", lambda: Fernet.generate_key().decode()),
            signup_code=os.environ.get("SIGNUP_CODE", ""),
            public_url=os.environ.get("PUBLIC_URL", "").rstrip("/"),
            demo=os.environ.get("ZANDA_CHECKIN_DEMO") == "1",
            allow_insecure_http=os.environ.get("ALLOW_INSECURE_HTTP") == "1",
            keys_from_env=bool(os.environ.get("SECRET_KEY") and os.environ.get("ENCRYPTION_KEY")),
            allow_open_signup=os.environ.get("ALLOW_OPEN_SIGNUP") == "1",
        )
        values.update(overrides)
        return cls(**values)
