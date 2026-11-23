"""Finding today's appointment for the person at the kiosk and marking it Arrived.

Matching rules (kept deliberately narrow):
- Only today's active, one-to-one client appointments at this kiosk's location.
- Only appointments starting between `minutes_early` from now and `minutes_late`
  ago, and still in a check-in-able state (Pending, Confirmed, or already Arrived).
- The date of birth must match exactly one client in that window. Zero or
  several matches go to reception, never to a guess.

Only the few profile fields the kiosk needs are kept in memory, and nothing
about clients is written to disk or logs.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .zanda import ZandaClient, ZandaError

log = logging.getLogger(__name__)

CHECKIN_STATES = {"Pending", "Confirmed", "Arrived"}
APPOINTMENTS_TTL = 60          # seconds to reuse today's appointment list
PROFILE_TTL = 6 * 60 * 60      # seconds to reuse a client's minimal profile
TOKEN_TTL = 120                # seconds a "Is this you?" screen stays valid
MAX_MISSES = 6                 # failed lookups ...
MISS_WINDOW = 10 * 60          # ... within this many seconds ...
LOCKOUT = 5 * 60               # ... send everyone to reception for this long


@dataclass
class Candidate:
    appointment_id: int
    client_id: int
    first_name: str
    last_initial: str
    practitioner: str
    starts_at: datetime
    already_arrived: bool
    see_reception: bool

    def public(self) -> dict:
        """What the iPad is allowed to see."""
        return {
            "firstName": self.first_name,
            "practitioner": self.practitioner,
            "time": format_time(self.starts_at),
            "seeReception": self.see_reception,
        }


@dataclass
class CheckinEvent:
    """Sent to listeners after a successful check-in (for the future Mac notifier)."""
    client_label: str          # e.g. "Dylan C."
    practitioner: str
    starts_at: datetime
    see_reception: bool


@dataclass
class LookupResult:
    status: str                 # "match" or "reception"
    token: str | None = None
    candidate: Candidate | None = None
    reason: str = ""


def format_time(value: datetime) -> str:
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'am' if value.hour < 12 else 'pm'}"


def parse_dob(text: str) -> date | None:
    """Accepts DDMMYYYY or DD/MM/YYYY (Australian order)."""
    digits = "".join(ch for ch in text or "" if ch.isdigit())
    if len(digits) != 8:
        return None
    try:
        dob = date(int(digits[4:]), int(digits[2:4]), int(digits[:2]))
    except ValueError:
        return None
    if dob.year < 1900 or dob > date.today():
        return None
    return dob


class CheckinService:
    def __init__(self, client: ZandaClient, *, location_id: int, time_zone: str,
                 minutes_early: int = 90, minutes_late: int = 30, reception_flag: str = "",
                 now: Callable[[], datetime] | None = None):
        self.client = client
        self.location_id = location_id
        self.tz = ZoneInfo(time_zone)
        self.minutes_early = minutes_early
        self.minutes_late = minutes_late
        self.reception_flag = (reception_flag or "").strip().lower()
        self._now = now or (lambda: datetime.now(self.tz))
        self._lock = threading.Lock()
        self._appointments: tuple[float, list[dict]] | None = None
        self._profiles: dict[int, tuple[float, dict]] = {}
        self._tokens: dict[str, tuple[float, Candidate]] = {}
        self._misses: list[float] = []
        self._locked_until = 0.0
        self.listeners: list[Callable[[CheckinEvent], None]] = []

    # -- data from Zanda ---------------------------------------------------
    def _todays_appointments(self) -> list[dict]:
        with self._lock:
            cached = self._appointments
            if cached and clock.monotonic() - cached[0] < APPOINTMENTS_TTL:
                return cached[1]
        today = self._now().date()
        appts = self.client.list_appointments(
            datetime.combine(today, time(0, 0)),
            datetime.combine(today, time(23, 59, 59)),
            self.location_id,
        )
        with self._lock:
            self._appointments = (clock.monotonic(), appts)
        return appts

    def _profile(self, client_id: int) -> dict:
        cached = self._profiles.get(client_id)
        if cached and clock.monotonic() - cached[0] < PROFILE_TTL:
            return cached[1]
        full = self.client.get_client_profile(client_id)
        minimal = {
            "dob": full.get("dateOfBirth"),
            "first": (full.get("preferredName") or full.get("firstName") or "").strip(),
            "last_initial": (full.get("lastName") or "").strip()[:1].upper(),
        }
        self._profiles[client_id] = (clock.monotonic(), minimal)
        return minimal

    def _starts_at(self, appt: dict) -> datetime | None:
        try:
            day = date.fromisoformat(appt["date"][:10])
            start = time.fromisoformat(appt["startAt"])
        except (KeyError, TypeError, ValueError):
            return None
        return datetime.combine(day, start, tzinfo=self.tz)

    def _eligible(self, appt: dict, now: datetime) -> datetime | None:
        if not appt.get("isActive", True):
            return None
        if appt.get("clientCapacity") != "Client Appointment":
            return None  # group and personal appointments go to reception
        if appt.get("attendanceState") not in CHECKIN_STATES:
            return None
        starts = self._starts_at(appt)
        if starts is None:
            return None
        if not (now - timedelta(minutes=self.minutes_late)
                <= starts <= now + timedelta(minutes=self.minutes_early)):
            return None
        return starts

    # -- lockout -----------------------------------------------------------
    def _record_miss(self):
        t = clock.monotonic()
        self._misses = [m for m in self._misses if t - m < MISS_WINDOW] + [t]
        if len(self._misses) >= MAX_MISSES:
            self._locked_until = t + LOCKOUT
            self._misses.clear()
            log.warning("Too many unmatched check-ins; sending everyone to reception for %s s", LOCKOUT)

    @property
    def locked(self) -> bool:
        return clock.monotonic() < self._locked_until

    # -- public API ----------------------------------------------------------
    def lookup(self, dob: date) -> LookupResult:
        if self.locked:
            return LookupResult("reception", reason="locked")
        now = self._now()
        matches: list[Candidate] = []
        for appt in self._todays_appointments():
            starts = self._eligible(appt, now)
            if starts is None:
                continue
            for c in appt.get("clients") or []:
                cid = c.get("id")
                if cid is None:
                    continue
                profile = self._profile(int(cid))
                if profile["dob"] and profile["dob"][:10] == dob.isoformat():
                    flag = (appt.get("flag") or "").strip().lower()
                    matches.append(Candidate(
                        appointment_id=int(appt["id"]),
                        client_id=int(cid),
                        first_name=profile["first"] or "there",
                        last_initial=profile["last_initial"],
                        practitioner=(appt.get("practitioner") or {}).get("name", ""),
                        starts_at=starts,
                        already_arrived=appt.get("attendanceState") == "Arrived",
                        see_reception=bool(self.reception_flag) and flag == self.reception_flag,
                    ))
        if len(matches) != 1:
            self._record_miss()
            return LookupResult("reception", reason="none" if not matches else "several")
        token = secrets.token_urlsafe(24)
        with self._lock:
            t = clock.monotonic()
            self._tokens = {k: v for k, v in self._tokens.items() if t - v[0] < TOKEN_TTL}
            self._tokens[token] = (t, matches[0])
        return LookupResult("match", token=token, candidate=matches[0])

    def confirm(self, token: str) -> Candidate | None:
        with self._lock:
            entry = self._tokens.pop(token or "", None)
        if not entry or clock.monotonic() - entry[0] > TOKEN_TTL:
            return None
        cand = entry[1]
        if not cand.already_arrived:
            self.client.mark_arrived(cand.appointment_id)
        with self._lock:
            self._appointments = None  # refresh next time so states are current
        label = f"{cand.first_name} {cand.last_initial}." if cand.last_initial else cand.first_name
        event = CheckinEvent(label, cand.practitioner, cand.starts_at, cand.see_reception)
        for listener in self.listeners:
            try:
                listener(event)
            except Exception:  # a broken listener must never block a check-in
                log.exception("Check-in listener failed")
        return cand

    def cancel(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token or "", None)


__all__ = ["CheckinService", "Candidate", "CheckinEvent", "LookupResult", "parse_dob", "ZandaError"]
