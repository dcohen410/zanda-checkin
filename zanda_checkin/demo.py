"""A pretend Zanda for trying the kiosk without an API key (and for tests).

Run with:  ZANDA_CHECKIN_DEMO=1 python -m zanda_checkin
Any API key is accepted. All names and dates below are made up.

Demo dates of birth (enter as DDMMYYYY):
  01/02/1990  Alex, seeing Sam Rivera in 20 minutes
  15/06/1985  Jordan, seeing Priya Shah in 45 minutes (flagged "See reception")
  03/03/2001  two people share this one, so it goes to reception
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .zanda import ZandaError

PROFILES = {
    101: {"id": 101, "firstName": "Alexandra", "preferredName": "Alex", "lastName": "Nguyen", "dateOfBirth": "1990-02-01"},
    102: {"id": 102, "firstName": "Jordan", "preferredName": None, "lastName": "Okafor", "dateOfBirth": "1985-06-15"},
    103: {"id": 103, "firstName": "Casey", "preferredName": None, "lastName": "Brown", "dateOfBirth": "2001-03-03"},
    104: {"id": 104, "firstName": "Riley", "preferredName": None, "lastName": "Brown", "dateOfBirth": "2001-03-03"},
    105: {"id": 105, "firstName": "Morgan", "preferredName": None, "lastName": "Lee", "dateOfBirth": "1978-11-30"},
}


class DemoZandaClient:
    def __init__(self, api_key: str = "", region: str = "au", time_zone: str = "Australia/Sydney", now=None):
        if api_key == "bad-key":
            raise ZandaError(401, "Unauthorized")
        self.tz = ZoneInfo(time_zone)
        self._now = now or (lambda: datetime.now(self.tz))
        self.arrived: set[int] = set()
        self.calls: list[tuple] = []

    def _appt(self, appt_id, client_ids, practitioner, minutes_from_now, state="Confirmed", flag=None,
              capacity="Client Appointment"):
        start = (self._now() + timedelta(minutes=minutes_from_now)).replace(second=0, microsecond=0)
        return {
            "id": appt_id, "date": start.date().isoformat(), "startAt": start.strftime("%H:%M:%S"),
            "endAt": (start + timedelta(minutes=50)).strftime("%H:%M:%S"),
            "attendanceState": "Arrived" if appt_id in self.arrived else state,
            "clientCapacity": capacity, "isActive": True, "flag": flag,
            "location": {"id": 1, "name": "Main clinic"},
            "practitioner": {"id": 7, "name": practitioner},
            "clients": [{"id": c} for c in client_ids],
        }

    def list_locations(self):
        self.calls.append(("list_locations",))
        return [{"id": 1, "name": "Main clinic", "isActive": True, "serviceModality": "In Person"},
                {"id": 2, "name": "Telehealth", "isActive": True, "serviceModality": "Telehealth Video Call"}]

    def list_appointment_flags(self):
        return [{"id": 1, "name": "See reception", "isActive": True, "colour": "#ff9900"}]

    def list_appointments(self, date_from, date_to, location_id=None):
        self.calls.append(("list_appointments", location_id))
        return [
            self._appt(5001, [101], "Sam Rivera", 20),
            self._appt(5002, [102], "Priya Shah", 45, flag="See reception"),
            self._appt(5003, [103], "Sam Rivera", 30),
            self._appt(5004, [104], "Priya Shah", 60),
            self._appt(5005, [105], "Sam Rivera", -180, state="Completed"),
        ]

    def get_client_profile(self, client_id):
        self.calls.append(("get_client_profile", client_id))
        return PROFILES[int(client_id)]

    def mark_arrived(self, appointment_id):
        self.calls.append(("mark_arrived", appointment_id))
        self.arrived.add(int(appointment_id))
        return {"id": appointment_id, "attendanceState": "Arrived"}
