from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from zanda_checkin.checkin import CheckinService, format_time, parse_dob
from zanda_checkin.demo import DemoZandaClient
from zanda_checkin.zanda import ZandaError

TZ = "Australia/Sydney"
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=ZoneInfo(TZ))


def make(**kw):
    client = DemoZandaClient(time_zone=TZ, now=lambda: NOW)
    svc = CheckinService(client, location_id=1, time_zone=TZ, now=lambda: NOW,
                         reception_flag=kw.pop("reception_flag", "See reception"), **kw)
    return client, svc


def test_parse_dob_accepts_australian_order():
    assert parse_dob("01021990") == date(1990, 2, 1)
    assert parse_dob("01/02/1990") == date(1990, 2, 1)


@pytest.mark.parametrize("bad", ["", "0102199", "31022000", "01011850", "01019999", "abc"])
def test_parse_dob_rejects_bad_input(bad):
    assert parse_dob(bad) is None


def test_format_time():
    assert format_time(datetime(2026, 1, 1, 14, 5)) == "2:05 pm"
    assert format_time(datetime(2026, 1, 1, 0, 30)) == "12:30 am"
    assert format_time(datetime(2026, 1, 1, 12, 0)) == "12:00 pm"


def test_single_match_shows_preferred_name_and_marks_arrived():
    client, svc = make()
    res = svc.lookup(date(1990, 2, 1))
    assert res.status == "match"
    assert res.candidate.public() == {
        "firstName": "Alex", "practitioner": "Sam Rivera", "time": "10:20 am", "seeReception": False,
    }
    events = []
    svc.listeners.append(events.append)
    cand = svc.confirm(res.token)
    assert cand.appointment_id == 5001
    assert ("mark_arrived", 5001) in client.calls
    assert events[0].client_label == "Alex N."


def test_token_is_single_use():
    client, svc = make()
    res = svc.lookup(date(1990, 2, 1))
    assert svc.confirm(res.token) is not None
    assert svc.confirm(res.token) is None
    assert svc.confirm("made-up") is None


def test_no_match_goes_to_reception():
    _, svc = make()
    assert svc.lookup(date(1999, 9, 9)).status == "reception"


def test_shared_dob_goes_to_reception_not_a_guess():
    _, svc = make()
    res = svc.lookup(date(2001, 3, 3))
    assert res.status == "reception" and res.reason == "several"


def test_completed_or_out_of_window_appointments_are_ignored():
    _, svc = make()
    assert svc.lookup(date(1978, 11, 30)).status == "reception"  # Completed, 3 hours ago


def test_window_limits_how_early_you_can_check_in():
    _, svc = make(minutes_early=10)
    assert svc.lookup(date(1990, 2, 1)).status == "reception"  # appointment is 20 min away


def test_reception_flag_still_checks_in_but_says_see_reception():
    client, svc = make()
    res = svc.lookup(date(1985, 6, 15))
    assert res.candidate.see_reception is True
    svc.confirm(res.token)
    assert ("mark_arrived", 5002) in client.calls


def test_reception_flag_off_by_default():
    _, svc = make(reception_flag="")
    assert svc.lookup(date(1985, 6, 15)).candidate.see_reception is False


def test_already_arrived_is_not_patched_again():
    client, svc = make()
    svc.confirm(svc.lookup(date(1990, 2, 1)).token)
    client.calls.clear()
    res = svc.lookup(date(1990, 2, 1))
    assert res.status == "match"
    svc.confirm(res.token)
    assert not [c for c in client.calls if c[0] == "mark_arrived"]


def test_group_appointments_go_to_reception():
    client, svc = make()
    original = client.list_appointments
    client.list_appointments = lambda *a, **k: [
        {**a_, "clientCapacity": "Group Appointment"} for a_ in original(*a, **k)]
    assert svc.lookup(date(1990, 2, 1)).status == "reception"


def test_repeated_misses_lock_to_reception():
    _, svc = make()
    for _ in range(6):
        svc.lookup(date(1999, 9, 9))
    assert svc.locked
    assert svc.lookup(date(1990, 2, 1)).reason == "locked"


def test_appointments_list_is_cached_between_lookups():
    client, svc = make()
    svc.lookup(date(1999, 9, 9))
    svc.lookup(date(1999, 9, 8))
    assert sum(1 for c in client.calls if c[0] == "list_appointments") == 1


def test_broken_listener_does_not_block_checkin():
    client, svc = make()
    svc.listeners.append(lambda e: 1 / 0)
    assert svc.confirm(svc.lookup(date(1990, 2, 1)).token) is not None


def test_zanda_error_on_mark_arrived_propagates():
    client, svc = make()
    def boom(_):
        raise ZandaError(409, "Conflict")
    client.mark_arrived = boom
    res = svc.lookup(date(1990, 2, 1))
    with pytest.raises(ZandaError):
        svc.confirm(res.token)
