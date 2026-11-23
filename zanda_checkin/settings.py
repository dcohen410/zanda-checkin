"""Service settings, all from environment variables (12-factor, container friendly).

DATA_DIR          Where SQLite and auto-generated keys live (default ./data). Mount a volume here.
DATABASE_URL      SQLAlchemy URL. Default: SQLite in DATA_DIR. Postgres: postgresql+psycopg://user:pass@host/db
SECRET_KEY        Signs session cookies. Auto-generated into DATA_DIR if unset.
ENCRYPTION_KEY    Fernet key that encrypts stored Zanda API keys. Auto-generated into DATA_DIR if unset.
                  In production set it explicitly and back it up: lose it and every stored key is unreadable.
SIGNUP_CODE       If set, new practices must enter this code to create an account (invite-only).
PUBLIC_URL        The service's public https URL (shown to users; used for secure cookies when https).
ZANDA_CHECKIN_DEMO=1  Use pretend Zanda data for every site (for demos and tests).
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

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

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        data_dir = Path(overrides.pop("data_dir", None) or os.environ.get("DATA_DIR", "data")).resolve()
        values = dict(
            data_dir=data_dir,
            database_url=os.environ.get("DATABASE_URL") or f"sqlite:///{data_dir / 'zanda-checkin.db'}",
            secret_key=os.environ.get("SECRET_KEY") or _persisted_secret(
                data_dir, "secret_key", lambda: secrets.token_hex(32)),
            encryption_key=os.environ.get("ENCRYPTION_KEY") or _persisted_secret(
                data_dir, "encryption_key", lambda: Fernet.generate_key().decode()),
            signup_code=os.environ.get("SIGNUP_CODE", ""),
            public_url=os.environ.get("PUBLIC_URL", "").rstrip("/"),
            demo=os.environ.get("ZANDA_CHECKIN_DEMO") == "1",
        )
        values.update(overrides)
        return cls(**values)
