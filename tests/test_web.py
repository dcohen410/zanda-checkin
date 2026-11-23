import asyncio
import json

from fastapi.testclient import TestClient

from conftest import add_site, csrf, kiosk_signin, signup
from zanda_checkin.db import Site


# -- accounts -------------------------------------------------------------------
def test_home_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_signup_and_dashboard(client):
    r = signup(client)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert "Add your first site" in client.get("/admin").text


def test_signup_validation(client):
    token = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": token, "practice_name": "P", "email": "a@b.co",
                                     "password": "short", "password2": "short"})
    assert r.status_code == 400 and "at least 10" in r.text


def test_duplicate_email(client):
    signup(client)
    client.post("/logout", data={"csrf": csrf(client, "/admin")})
    assert "already an account" in signup(client).text


def test_signup_code_required_when_set(app, client):
    app.state.settings.signup_code = "LETMEIN"
    r = signup(client)
    assert r.status_code == 400 and "sign-up code" in r.text


def test_csrf_required(client):
    r = client.post("/signup", data={"practice_name": "P", "email": "a@b.co",
                                     "password": "x" * 12, "password2": "x" * 12})
    assert r.status_code == 400


def test_login_wrong_password_and_rate_limit(client):
    signup(client)
    client.post("/logout", data={"csrf": csrf(client, "/admin")})
    for _ in range(10):
        r = client.post("/login", data={"csrf": csrf(client, "/login"), "email": "owner@example.com",
                                        "password": "wrong wrong wrong"})
    assert "Too many sign-in attempts" in client.post(
        "/login", data={"csrf": csrf(client, "/login"), "email": "owner@example.com",
                        "password": "correct horse battery"}).text


def test_admin_requires_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


# -- sites --------------------------------------------------------------------------
def test_add_site_encrypts_key_and_picks_location(app, client):
    signup(client)
    r = add_site(client, key="super-secret-key-1234")
    assert r.status_code == 303
    db = app.state.Session()
    site = db.get(Site, 1)
    assert site.location_id == 1 and site.location_name == "Main clinic"
    assert "super-secret" not in site.zanda_key_encrypted and site.zanda_key_hint == "1234"
    assert app.state.keybox.decrypt(site.zanda_key_encrypted) == "super-secret-key-1234"
    page = client.get("/admin/sites/1").text
    assert "super-secret-key" not in page and "1234" in page


def test_add_site_rejects_bad_key(client):
    signup(client)
    r = add_site(client, key="bad-key")
    assert r.status_code == 400 and "didn&#39;t accept" in r.text


def test_other_practice_cannot_see_site(client):
    signup(client)
    add_site(client)
    client.post("/logout", data={"csrf": csrf(client, "/admin")})
    signup(client, email="other@example.com", practice="Other")
    assert client.get("/admin/sites/1").status_code == 404


def test_save_site_settings(app, client):
    signup(client)
    add_site(client)
    token = csrf(client, "/admin/sites/1")
    client.post("/admin/sites/1", data={"csrf": token, "name": "Deakin", "welcome_name": "Welcome",
                                        "location_id": "1", "time_zone": "Australia/Sydney",
                                        "minutes_early": "60", "minutes_late": "10",
                                        "reception_flag": "See reception", "require_surname_initial": "on",
                                        "mark_arrived": "on"})
    site = app.state.Session().get(Site, 1)
    assert site.name == "Deakin" and site.minutes_early == 60 and site.require_surname_initial
    assert site.show_clinician is False and site.mark_arrived is True


def test_delete_site_needs_name(app, client):
    signup(client)
    add_site(client)
    token = csrf(client, "/admin/sites/1")
    assert client.post("/admin/sites/1/delete", data={"csrf": token, "confirm_name": "nope"}).status_code == 400
    r = client.post("/admin/sites/1/delete", data={"csrf": token, "confirm_name": "Main"}, follow_redirects=False)
    assert r.status_code == 303 and app.state.Session().get(Site, 1) is None


# -- check-in portal -------------------------------------------------------------
def test_kiosk_redirects_to_login_without_device(client):
    r = client.get("/checkin", follow_redirects=False)
    assert r.headers["location"] == "/checkin/login"


def test_kiosk_end_to_end(client):
    signup(client)
    add_site(client)
    r = kiosk_signin(client)
    assert r.status_code == 303 and "zc_kiosk" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and "Path=/checkin" in r.headers["set-cookie"]
    assert "Test Practice" in client.get("/checkin").text
    res = client.post("/checkin/api/lookup", json={"dob": "01021990"}).json()
    assert res["status"] == "match" and res["person"]["firstName"] == "Alex"
    done = client.post("/checkin/api/confirm", json={"token": res["token"]}).json()
    assert done == {"status": "done", "firstName": "Alex", "seeReception": False}


def test_kiosk_needs_json(client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    assert client.post("/checkin/api/lookup", data={"dob": "01021990"}).status_code == 400


def test_kiosk_api_without_device_is_401(client):
    assert client.post("/checkin/api/lookup", json={"dob": "01021990"}).status_code == 401


def test_kiosk_requires_initial_when_site_asks(app, client):
    signup(client)
    add_site(client)
    db = app.state.Session()
    db.get(Site, 1).require_surname_initial = True
    db.commit()
    kiosk_signin(client)
    assert client.post("/checkin/api/lookup", json={"dob": "01021990"}).json()["status"] == "need_initial"
    assert client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "Z"}).json()["status"] == "reception"
    assert client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "n"}).json()["status"] == "match"


def test_shared_dob_resolved_by_initial(app, client):
    signup(client)
    add_site(client)
    db = app.state.Session()
    db.get(Site, 1).require_surname_initial = True
    db.commit()
    kiosk_signin(client)
    # Casey and Riley Brown share a DOB and initial, so still reception
    assert client.post("/checkin/api/lookup", json={"dob": "03032001", "initial": "B"}).json()["status"] == "reception"


def test_hide_clinician(app, client):
    signup(client)
    add_site(client)
    db = app.state.Session()
    db.get(Site, 1).show_clinician = False
    db.commit()
    kiosk_signin(client)
    assert client.post("/checkin/api/lookup", json={"dob": "01021990"}).json()["person"]["practitioner"] == ""


def test_kiosk_cannot_reach_admin(client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    client.post("/logout", data={"csrf": csrf(client, "/checkin/login")})
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_kiosk_exit_signs_out_device(client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    token = csrf(client, "/checkin/exit")
    r = client.post("/checkin/exit", data={"csrf": token, "email": "owner@example.com",
                                           "password": "correct horse battery"}, follow_redirects=False)
    assert r.headers["location"] == "/checkin/login"
    assert client.post("/checkin/api/lookup", json={"dob": "01021990"}).status_code == 401


def test_revoked_kiosk_is_signed_out(client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    signup_client_csrf = csrf(client, "/admin")
    client.post("/admin/devices/1/revoke", data={"csrf": signup_client_csrf})
    assert client.get("/checkin", follow_redirects=False).headers["location"] == "/checkin/login"


# -- desktop API ---------------------------------------------------------------------
def api_login(client):
    r = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "correct horse battery",
                                                "deviceName": "Reception Mac"})
    assert r.status_code == 200
    return r.json()["token"]


def test_api_login_and_sites(client):
    signup(client)
    add_site(client)
    token = api_login(client)
    assert token.startswith("zc_")
    sites = client.get("/api/v1/sites", headers={"Authorization": f"Bearer {token}"}).json()["sites"]
    assert sites == [{"id": 1, "name": "Main", "locationName": "Main clinic", "ready": True}]


def test_api_bad_login(client):
    signup(client)
    r = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "nope nope nope"})
    assert r.status_code == 401


def test_api_requires_token(client):
    assert client.get("/api/v1/sites").status_code == 401
    assert client.get("/api/v1/sites", headers={"Authorization": "Bearer zc_fake"}).status_code == 401


def test_api_logout_revokes(client):
    signup(client)
    token = api_login(client)
    h = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/v1/auth/logout", headers=h).status_code == 200
    assert client.get("/api/v1/sites", headers=h).status_code == 401


def test_events_other_practice_404(client):
    signup(client)
    add_site(client)
    client.post("/logout", data={"csrf": csrf(client, "/admin")})
    signup(client, email="other@example.com", practice="Other")
    token = client.post("/api/v1/auth/login", json={"email": "other@example.com",
                                                    "password": "correct horse battery"}).json()["token"]
    assert client.get("/api/v1/sites/1/events", headers={"Authorization": f"Bearer {token}"}).status_code == 404


def test_checkin_is_broadcast_to_site_subscribers(app, client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    received = []

    async def listen_then_checkin():
        q = app.state.broadcaster.subscribe(1)
        loop = asyncio.get_running_loop()

        def do_checkin():
            res = client.post("/checkin/api/lookup", json={"dob": "15061985"}).json()
            client.post("/checkin/api/confirm", json={"token": res["token"]})

        await loop.run_in_executor(None, do_checkin)
        received.append(json.loads(await asyncio.wait_for(q.get(), 5)))

    asyncio.run(listen_then_checkin())
    assert received[0]["message"] == "Jordan O. has arrived for Priya Shah"
    assert received[0]["siteId"] == 1 and received[0]["zandaUpdated"] is True


def test_security_headers(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_api_key_never_in_pages(client):
    signup(client)
    add_site(client, key="super-secret-key-999")
    kiosk_signin(client)
    for url in ["/admin", "/admin/sites/1", "/checkin", "/checkin/manifest.webmanifest", "/healthz"]:
        assert "super-secret-key" not in client.get(url).text
