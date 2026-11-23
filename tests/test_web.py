import re

import pytest

from zanda_checkin import create_app
from zanda_checkin.config import Config
from zanda_checkin.demo import DemoZandaClient


@pytest.fixture
def cfg(tmp_path):
    return Config(tmp_path / "config.json")


@pytest.fixture
def client(cfg):
    app = create_app(cfg, client_factory=DemoZandaClient)
    app.testing = True
    return app.test_client()


def csrf(client, url):
    html = client.get(url).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def do_setup(client, key="good-key", pin="2468"):
    token = csrf(client, "/setup")
    return client.post("/setup", data={"csrf": token, "api_key": key, "region": "au",
                                       "practice_name": "Test Practice", "pin": pin, "pin2": pin})


def test_fresh_install_redirects_to_setup(client):
    resp = client.get("/")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/setup")


def test_setup_rejects_bad_key(client, cfg):
    resp = do_setup(client, key="bad-key")
    assert b"didn&#39;t accept that key" in resp.data
    assert not cfg.has_api_key


def test_setup_saves_key_and_picks_only_in_person_location(client, cfg):
    resp = do_setup(client)
    assert resp.status_code == 302
    assert cfg["api_key"] == "good-key"
    assert cfg["location_id"] == 1  # Telehealth location is skipped
    assert cfg["admin_pin_hash"] and cfg["admin_pin_hash"] != "2468"
    assert cfg.path.stat().st_mode & 0o777 == 0o600


def test_setup_requires_csrf(client):
    resp = client.post("/setup", data={"api_key": "x", "pin": "1234", "pin2": "1234"})
    assert resp.status_code == 400


def test_kiosk_flow_end_to_end(client, cfg):
    do_setup(client)
    client.post("/admin/logout")
    page = client.get("/")
    assert b"Test Practice" in page.data
    res = client.post("/api/lookup", json={"dob": "01021990"}).get_json()
    assert res["status"] == "match"
    assert set(res["person"]) == {"firstName", "practitioner", "time", "seeReception"}
    done = client.post("/api/confirm", json={"token": res["token"]}).get_json()
    assert done == {"status": "done", "firstName": "Alex", "seeReception": False}


def test_lookup_never_leaks_why_on_reception(client):
    do_setup(client)
    res = client.post("/api/lookup", json={"dob": "09091999"}).get_json()
    assert res["status"] == "reception" and "person" not in res


def test_lookup_requires_json(client):
    do_setup(client)
    assert client.post("/api/lookup", data={"dob": "01021990"}).status_code == 400


def test_invalid_dob(client):
    do_setup(client)
    assert client.post("/api/lookup", json={"dob": "31029999"}).get_json()["status"] == "invalid"


def test_admin_needs_pin(client):
    do_setup(client)
    client.post("/admin/logout")
    assert client.get("/admin").headers["Location"].endswith("/admin/login")
    token = csrf(client, "/admin/login")
    assert b"didn&#39;t match" in client.post("/admin/login", data={"csrf": token, "pin": "0000"}).data
    token = csrf(client, "/admin/login")
    assert client.post("/admin/login", data={"csrf": token, "pin": "2468"}).status_code == 302
    assert client.get("/admin").status_code == 200


def test_admin_pin_lockout(client):
    do_setup(client)
    client.post("/admin/logout")
    for _ in range(5):
        token = csrf(client, "/admin/login")
        client.post("/admin/login", data={"csrf": token, "pin": "0000"})
    token = csrf(client, "/admin/login")
    resp = client.post("/admin/login", data={"csrf": token, "pin": "2468"})
    assert b"Too many tries" in resp.data


def test_admin_saves_reception_flag(client, cfg):
    do_setup(client)
    token = csrf(client, "/admin")
    client.post("/admin", data={"csrf": token, "location_id": "1", "location_name_1": "Main clinic",
                                "practice_name": "X", "minutes_early": "60", "minutes_late": "15",
                                "reception_flag": "See reception"})
    assert cfg["reception_flag"] == "See reception"
    assert cfg["minutes_early"] == 60 and cfg["location_name"] == "Main clinic"


def test_forget_key(client, cfg):
    do_setup(client)
    token = csrf(client, "/admin")
    client.post("/admin/forget-key", data={"csrf": token})
    assert not cfg.has_api_key
    assert client.get("/").headers["Location"].endswith("/setup")


def test_api_key_never_appears_in_pages(client):
    do_setup(client, key="super-secret-key-123")
    for url in ["/", "/admin", "/manifest.webmanifest", "/healthz"]:
        assert b"super-secret-key-123" not in client.get(url).data


def test_manifest_is_standalone(client):
    do_setup(client)
    data = client.get("/manifest.webmanifest").get_json()
    assert data["display"] == "standalone"
