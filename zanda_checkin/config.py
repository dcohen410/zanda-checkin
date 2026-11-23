"""Settings stored on the server Mac, never in the repo.

Default location: ~/.zanda-checkin/config.json (override with ZANDA_CHECKIN_HOME).
The file holds the Zanda API key, so it is written with owner-only permissions.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path

DEFAULTS = {
    "api_key": "",
    "region": "au",
    "time_zone": "Australia/Sydney",
    "location_id": None,
    "location_name": "",
    "practice_name": "",
    "admin_pin_hash": "",
    # How early / late (minutes) someone can check in for their appointment.
    "minutes_early": 90,
    "minutes_late": 30,
    # Appointments with this Zanda appointment flag still check in, but the
    # client is asked to see reception afterwards. Blank = off.
    "reception_flag": "",
    "secret_key": "",
}


def data_dir() -> Path:
    return Path(os.environ.get("ZANDA_CHECKIN_HOME", Path.home() / ".zanda-checkin"))


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "config.json"
        self.values = dict(DEFAULTS)
        if self.path.exists():
            self.values.update(json.loads(self.path.read_text()))
        if not self.values["secret_key"]:
            self.values["secret_key"] = secrets.token_hex(32)
            self.save()

    def __getitem__(self, key):
        return self.values[key]

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, **changes):
        self.values.update(changes)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.values, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @property
    def has_api_key(self) -> bool:
        return bool(self.values["api_key"])

    @property
    def is_ready(self) -> bool:
        return self.has_api_key and bool(self.values["location_id"]) and bool(self.values["admin_pin_hash"])
