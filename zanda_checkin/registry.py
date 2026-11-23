"""Per-site check-in services and the live event broadcaster for desktop notifiers.

The broadcaster is in-memory, so run a single app process (one container, one
uvicorn worker). That comfortably serves many practices. To scale out to several
processes, swap Broadcaster for one backed by Redis or Postgres LISTEN/NOTIFY.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict
from dataclasses import dataclass

from .checkin import CheckinEvent, CheckinService
from .db import Site
from .security import KeyBox
from .zanda import ZandaClient

log = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self):
        self._subs: dict[int, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = defaultdict(set)
        self._lock = threading.Lock()

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
    return {
        "type": "checkin",
        "siteId": site.id,
        "siteName": site.name,
        "client": event.client_label,
        "practitioner": event.practitioner,
        "time": event.starts_at.strftime("%H:%M"),
        "seeReception": event.see_reception,
        "zandaUpdated": event.zanda_updated,
        "message": f"{event.client_label} has arrived for {event.practitioner}",
    }


@dataclass
class _Entry:
    version: str
    service: CheckinService


class ServiceRegistry:
    """Builds one CheckinService per site, rebuilt whenever the site's settings change."""

    def __init__(self, keybox: KeyBox, broadcaster: Broadcaster, client_factory=ZandaClient):
        self.keybox = keybox
        self.broadcaster = broadcaster
        self.client_factory = client_factory
        self._entries: dict[int, _Entry] = {}
        self._lock = threading.Lock()

    def client_for(self, site: Site, api_key: str | None = None):
        key = api_key if api_key is not None else self.keybox.decrypt(site.zanda_key_encrypted)
        return self.client_factory(key, site.zanda_region, site.time_zone)

    def service_for(self, site: Site) -> CheckinService:
        version = f"{site.updated_at.isoformat() if site.updated_at else ''}:{site.zanda_key_encrypted[-12:]}"
        with self._lock:
            entry = self._entries.get(site.id)
            if entry and entry.version == version:
                return entry.service
        svc = CheckinService(
            self.client_for(site),
            location_id=int(site.location_id),
            time_zone=site.time_zone,
            minutes_early=site.minutes_early,
            minutes_late=site.minutes_late,
            reception_flag=site.reception_flag,
            mark_arrived=site.mark_arrived,
        )
        site_snapshot = Site(id=site.id, name=site.name)
        svc.listeners.append(lambda e: self.broadcaster.publish(site_snapshot.id, event_payload(site_snapshot, e)))
        with self._lock:
            self._entries[site.id] = _Entry(version, svc)
        return svc

    def forget(self, site_id: int) -> None:
        with self._lock:
            self._entries.pop(site_id, None)
