"""Minimal client for the Zanda public API (v1, beta).

Only the endpoints the kiosk needs are wrapped here, on purpose. A Zanda API
key can read the whole practice (including clinical notes), so this module is
an allow-list: if an endpoint isn't in this file, the app can't call it.

Reference: https://zandaapi.zandahealth.com/docs/
"""
from __future__ import annotations

import time
from datetime import datetime

import requests

REGIONS = {
    "au": "https://zandaapi.zandahealth.com/api/v1",
    "us": "https://zandaapi.us.zandahealth.com/api/v1",
    "uk": "https://zandaapi.uk.zandahealth.com/api/v1",
}

# The non-HATEOAS media type: same data, without the "links" blocks.
ACCEPT = "application/vnd.zandaapi+json"
TIMEOUT = 15
MAX_RETRY_WAIT = 10


class ZandaError(Exception):
    def __init__(self, status: int, title: str = "", detail: str = ""):
        self.status = status
        self.title = title
        self.detail = detail
        super().__init__(f"Zanda API {status}: {title or detail or 'error'}")

    @property
    def is_auth_problem(self) -> bool:
        return self.status in (401, 403)


def zanda_datetime(value: datetime) -> str:
    """Zanda wants wall-clock date-times with no offset (zone comes from X-Time-Zone)."""
    return value.replace(tzinfo=None, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")


class ZandaClient:
    def __init__(self, api_key: str, region: str = "au", time_zone: str = "Australia/Sydney",
                 session: requests.Session | None = None):
        if region not in REGIONS:
            raise ValueError(f"Unknown region {region!r}")
        self.base_url = REGIONS[region]
        self.session = session or requests.Session()
        self.session.headers.update({
            "X-API-Key": api_key,
            "Accept": ACCEPT,
            "X-Time-Zone": time_zone,
        })

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, path: str, *, params=None, json=None):
        url = f"{self.base_url}{path}"
        for attempt in range(3):
            resp = self.session.request(method, url, params=params, json=json, timeout=TIMEOUT)
            if resp.status_code == 429 and attempt < 2:
                wait = resp.headers.get("Retry-After")
                try:
                    delay = min(float(wait), MAX_RETRY_WAIT) if wait else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                time.sleep(delay)
                continue
            break
        if resp.status_code >= 400:
            title = detail = ""
            try:
                body = resp.json()
                title = body.get("title") or ""
                detail = body.get("detail") or ""
            except ValueError:
                pass
            raise ZandaError(resp.status_code, title, detail)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _paged(self, path: str, params: dict | None = None):
        params = dict(params or {})
        params["pageSize"] = 100
        params["cursor"] = ""
        while True:
            page = self._request("GET", path, params=params)
            for item in page.get("items", []):
                yield item.get("data", item)
            if not page.get("hasNextPage") or not page.get("nextCursor"):
                return
            params["cursor"] = page["nextCursor"]

    # -- endpoints the kiosk uses -----------------------------------------
    def list_locations(self) -> list[dict]:
        return list(self._paged("/locations", {"isActive": "true"}))

    def list_appointment_flags(self) -> list[dict]:
        return list(self._paged("/appointment-flags", {"isActive": "true"}))

    def list_appointments(self, date_from: datetime, date_to: datetime,
                          location_id: int | None = None) -> list[dict]:
        params = {
            "isActive": "true",
            "dateFrom": zanda_datetime(date_from),
            "dateTo": zanda_datetime(date_to),
            "sort": "dateFrom:asc",
        }
        if location_id:
            params["locationId"] = location_id
        return list(self._paged("/appointments", params))

    def get_client_profile(self, client_id: int) -> dict:
        return self._request("GET", f"/client-profiles/{int(client_id)}")["data"]

    def mark_arrived(self, appointment_id: int) -> dict:
        body = self._request("PATCH", f"/appointments/{int(appointment_id)}",
                             json={"attendanceState": "Arrived"})
        return (body or {}).get("data", {})
