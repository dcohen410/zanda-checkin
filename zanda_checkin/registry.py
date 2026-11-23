"""Per-site check-in services and the live event broadcaster for desktop notifiers.

The broadcaster is in-memory, so run a single app process (one container, one
uvicorn worker). That comfortably serves many practices. To scale out to several
processes, swap Broadcaster for one backed by Redis or Postgres LISTEN/NOTIFY.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

from .checkin import CheckinEvent, CheckinService
from .db import Site
from .zanda import ZandaClient

log = logging.getLogger(__name__)


MAX_STREAMS_PER_DEVICE = 3


class Broadcaster:
    def __init__(self):
        self._subs: dict[int, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = defaultdict(set)
        self._per_device: dict[int, int] = defaultdict(int)
        self._lock = threading.Lock()

    def claim(self, device_id: int) -> bool:
        """Limit live connections per signed-in desktop app."""
        with self._lock:
            if self._per_device[device_id] >= MAX_STREAMS_PER_DEVICE:
                return False
            self._per_device[device_id] += 1
            return True

    def release(self, device_id: int) -> None:
        with self._lock:
            self._per_device[device_id] = max(0, self._per_device[device_id] - 1)

    def subscribe(self, site_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subs[site_id].add((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, site_id: int, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs[site_id] = {s for s in self._subs[site_id] if s[1] is not q}

    def subscriber_count(self, site_id: int) -> int:
        with self._lock:
            return len(self._subs.get(site_id, ()))

    def publish(self, site_id: int, payload: dict) -> None:
        """Safe to call from any thread."""
        data = json.dumps(payload)
        with self._lock:
            subs = list(self._subs.get(site_id, ()))
        for loop, q in subs:
            def put(q=q):
                if q.full():
                    try:
                        q.get_nowait()  # drop the oldest rather than block
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(data)
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:  # loop closed
                pass


def event_payload(site: Site, event: CheckinEvent) -> dict:
    client = event.client_label if site.notify_names else "A client"
    return {
        "type": "checkin",
        "siteId": site.id,
        "siteName": site.name,
        "client": client,
        "practitioner": event.practitioner,
        "time": event.starts_at.strftime("%H:%M"),
        "seeReception": event.see_reception,
        "zandaUpdated": event.zanda_updated,
        "message": f"{client} has arrived for {event.practitioner}",
    }


@dataclass
class _Entry:
    version: str
    service: CheckinService
    last_used: float


IDLE_EVICT = 15 * 60  # forget a site's in-memory Zanda client (and key) after 15 idle minutes


class ServiceRegistry:
    """One CheckinService per site. The Zanda key is supplied by the caller on each request
    (unlocked from the practice's device or session) and only kept in memory while in use."""

    def __init__(self, broadcaster: Broadcaster, client_factory=ZandaClient):
        self.broadcaster = broadcaster
        self.client_factory = client_factory
        self._entries: dict[int, _Entry] = {}
        # Kiosk lockout state per site, kept when a site's service is rebuilt or evicted.
        self._lockouts: dict[int, tuple[list[float], float]] = {}
        self._lock = threading.Lock()

    def client_for(self, site: Site, api_key: str):
        return self.client_factory(api_key, site.zanda_region, site.time_zone)

    def service_for(self, site: Site, api_key: str) -> CheckinService:
        fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        version = f"{site.updated_at.isoformat() if site.updated_at else ''}:{fingerprint}"
        now = time.monotonic()
        with self._lock:
            self._evict_idle(now)
            entry = self._entries.get(site.id)
            if entry and entry.version == version:
                entry.last_used = now
                return entry.service
        svc = CheckinService(
            self.client_for(site, api_key),
            location_id=int(site.location_id),
            time_zone=site.time_zone,
            minutes_early=site.minutes_early,
            minutes_late=site.minutes_late,
            reception_flag=site.reception_flag,
            mark_arrived=site.mark_arrived,
        )
        site_snapshot = Site(id=site.id, name=site.name, notify_names=site.notify_names)
        svc.listeners.append(lambda e: self.broadcaster.publish(site_snapshot.id, event_payload(site_snapshot, e)))
        with self._lock:
            old = self._entries.get(site.id)
            if old is not None:
                self._save_lockout(site.id, old.service)
            svc._misses, svc._locked_until = self._lockouts.get(site.id, ([], 0.0))
            self._entries[site.id] = _Entry(version, svc, now)
        return svc

    def _save_lockout(self, site_id: int, svc: CheckinService) -> None:
        self._lockouts[site_id] = (list(svc._misses), svc._locked_until)

    def _evict_idle(self, now: float) -> None:
        for sid in [k for k, e in self._entries.items() if now - e.last_used > IDLE_EVICT]:
            self._save_lockout(sid, self._entries[sid].service)
            del self._entries[sid]

    def forget(self, site_id: int) -> None:
        with self._lock:
            old = self._entries.pop(site_id, None)
            if old is not None:
                self._save_lockout(site_id, old.service)
