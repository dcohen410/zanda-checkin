import asyncio
import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from conftest import PASSWORD, add_site, api_login, csrf, kiosk_signin, login, next_code, signup
from zanda_checkin.app import create_app
from zanda_checkin.db import Site
from zanda_checkin.demo import DemoZandaClient
from zanda_checkin.settings import Settings


def logout(client):
    client.post("/logout", data={"csrf": csrf(client, "/login")})


# -- accounts -------------------------------------------------------------------
def test_home_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_signup_requires_mfa_before_admin(client):
    r = signup(client, enrol=False)
    assert r.headers["location"] == "/admin/mfa"
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/admin/mfa"
    assert "Scan this" in client.get("/admin/mfa").text


def test_signup_then_dashboard(client):
    signup(client)
    assert "Add your first site" in client.get("/admin").text


def test_signup_validation(client):
    token = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": token, "practice_name": "P", "email": "a@b.co",
                                     "password": "short", "password2": "short"})
    assert r.status_code == 400 and "at least 12" in r.text


def test_duplicate_email(client):
    signup(client)
    logout(client)
    assert "already an account" in signup(client, enrol=False).text


def test_signup_code_required_when_set(app, client):
    app.state.settings.signup_code = "LETMEIN"
    r = signup(client, enrol=False)
    assert r.status_code == 400 and "sign-up code" in r.text


def test_csrf_required(client):
    r = client.post("/signup", data={"practice_name": "P", "email": "a@b.co",
                                     "password": "x" * 12, "password2": "x" * 12})
    assert r.status_code == 400


def test_login_needs_code_and_rejects_wrong_or_replayed_code(client):
    signup(client)
    logout(client)
    assert "6-digit code" in login(client, "/login", code="").text
    assert "didn&#39;t work" in login(client, "/login", code="000000").text
    code = next_code()
    assert login(client, "/login", code=code).status_code == 303
    logout(client)
    assert "didn&#39;t work" in login(client, "/login", code=code).text  # replay


def test_recovery_code_works_once(client):
    signup(client)
    page = client.post("/admin/mfa/recovery-codes", data={"csrf": csrf(client, "/admin/mfa"), "password": PASSWORD}).text
    import re
    rc = re.findall(r"<code>([A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4})</code>", page)[0]
    logout(client)
    assert login(client, "/login", code=rc).status_code == 303
    logout(client)
    assert login(client, "/login", code=rc).status_code == 400


def test_login_rate_limit(client):
    signup(client)
    logout(client)
    for _ in range(10):
        login(client, "/login", password="wrong wrong wrong", code="")
    assert "Too many sign-in attempts" in login(client, "/login").text


def test_admin_requires_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_logout_ends_server_session(app, client):
    signup(client)
    cookie = client.cookies.get("zc_session")
    logout(client)
    other = TestClient(app)
    other.cookies.set("zc_session", cookie)
    assert other.get("/admin", follow_redirects=False).headers["location"] == "/login"


def test_password_change_signs_out_other_sessions_and_keeps_keys(app, client):
    signup(client)
    add_site(client)
    kiosk = TestClient(app)
    kiosk_signin(kiosk)
    other = TestClient(app)
    login(other, "/login")
    assert other.get("/admin").status_code == 200
    r = client.post("/admin/account", data={"csrf": csrf(client, "/admin/account"), "current": PASSWORD,
                                            "new": "quartz meadow lantern ninety", "new2": "quartz meadow lantern ninety"})
    assert "signed out everywhere else" in r.text
    assert other.get("/admin", follow_redirects=False).status_code == 303
    # the iPad keeps working, and the new password unlocks the practice key
    assert kiosk.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).json()["status"] == "match"
    logout(client)
    login(client, "/login", password="quartz meadow lantern ninety")
    assert "Main clinic" in client.get("/admin/sites/1").text


# -- sites --------------------------------------------------------------------------
def test_add_site_and_key_never_shown(app, client):
    signup(client)
    r = add_site(client, key="super-secret-key-1234")
    assert r.status_code == 303
    site = app.state.Session().get(Site, 1)
    assert site.location_id == 1 and site.location_name == "Main clinic" and site.zanda_key_hint == "1234"
    page = client.get("/admin/sites/1").text
    assert "super-secret-key" not in page and "1234" in page


def test_add_site_rejects_bad_key(client):
    signup(client)
    r = add_site(client, key="bad-key")
    assert r.status_code == 400 and "didn&#39;t accept" in r.text


def test_other_practice_cannot_see_site(client):
    signup(client)
    add_site(client)
    logout(client)
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


def test_save_site_rejects_unknown_location(client):
    signup(client)
    add_site(client)
    r = client.post("/admin/sites/1", data={"csrf": csrf(client, "/admin/sites/1"), "name": "X",
                                            "location_id": "999", "time_zone": "Australia/Sydney"})
    assert r.status_code == 400


def test_delete_site_needs_name(app, client):
    signup(client)
    add_site(client)
    token = csrf(client, "/admin/sites/1")
    assert client.post("/admin/sites/1/delete", data={"csrf": token, "confirm_name": "nope"}).status_code == 400
    r = client.post("/admin/sites/1/delete", data={"csrf": token, "confirm_name": "Main"}, follow_redirects=False)
    assert r.status_code == 303 and app.state.Session().get(Site, 1) is None


# -- check-in portal -------------------------------------------------------------
def test_kiosk_redirects_to_login_without_device(client):
    assert client.get("/checkin", follow_redirects=False).headers["location"] == "/checkin/login"


def test_kiosk_signin_requires_mfa(client):
    signup(client, enrol=False)
    r = login(client, "/checkin/login", code="")
    assert "Set up two-step sign-in" in r.text


def test_kiosk_end_to_end(client):
    signup(client)
    add_site(client)
    r = kiosk_signin(client)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "zc_kiosk" in cookie and "HttpOnly" in cookie and "Path=/checkin" in cookie and "SameSite=strict" in cookie
    assert "Test Practice" in client.get("/checkin").text
    res = client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).json()
    assert res["status"] == "match" and res["person"]["firstName"] == "Alex"
    done = client.post("/checkin/api/confirm", json={"token": res["token"]}).json()
    assert done == {"status": "done", "firstName": "Alex", "seeReception": False}


def test_kiosk_needs_json(client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    assert client.post("/checkin/api/lookup", data={"dob": "01021990", "initial": "N"}).status_code == 400


def test_kiosk_api_without_device_is_401(client):
    assert client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).status_code == 401


def test_kiosk_requires_initial_by_default(app, client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    assert client.post("/checkin/api/lookup", json={"dob": "01021990"}).json()["status"] == "need_initial"
    assert client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "Z"}).json()["status"] == "reception"
    assert client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "n"}).json()["status"] == "match"


def test_initial_can_be_turned_off(app, client):
    signup(client)
    add_site(client)
    db = app.state.Session()
    db.get(Site, 1).require_surname_initial = False
    db.commit()
    kiosk_signin(client)
    assert client.post("/checkin/api/lookup", json={"dob": "01021990"}).json()["status"] == "match"


def test_clinician_hidden_by_default(client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    assert client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).json()["person"]["practitioner"] == ""


def test_kiosk_cannot_reach_admin(app, client):
    signup(client)
    add_site(client)
    ipad = TestClient(app)
    kiosk_signin(ipad)
    assert ipad.get("/admin", follow_redirects=False).status_code == 303


def test_kiosk_exit_signs_out_device(client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    r = login(client, "/checkin/exit")
    assert r.headers["location"] == "/checkin/login"
    assert client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).status_code == 401


def test_revoked_kiosk_is_signed_out(app, client):
    signup(client)
    add_site(client)
    ipad = TestClient(app)
    kiosk_signin(ipad)
    client.post("/admin/devices/1/revoke", data={"csrf": csrf(client, "/admin")})
    assert ipad.get("/checkin", follow_redirects=False).headers["location"] == "/checkin/login"
    assert ipad.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).status_code == 401


# -- desktop API ---------------------------------------------------------------------
def test_api_login_and_sites(client):
    signup(client)
    add_site(client)
    token = api_login(client)
    assert token.startswith("zc_")
    sites = client.get("/api/v1/sites", headers={"Authorization": f"Bearer {token}"}).json()["sites"]
    assert sites == [{"id": 1, "name": "Main", "locationName": "Main clinic", "ready": True}]


def test_api_login_needs_code(client):
    signup(client)
    r = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    assert r.status_code == 401 and r.json()["needsCode"] is True


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
    logout(client)
    signup(client, email="other@example.com", practice="Other")
    token = api_login(client, email="other@example.com")
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
            res = client.post("/checkin/api/lookup", json={"dob": "15061985", "initial": "O"}).json()
            client.post("/checkin/api/confirm", json={"token": res["token"]})

        await loop.run_in_executor(None, do_checkin)
        received.append(json.loads(await asyncio.wait_for(q.get(), 5)))

    asyncio.run(listen_then_checkin())
    assert received[0]["message"] == "Jordan O. has arrived for Priya Shah"


# -- zero-access encryption --------------------------------------------------------------
def test_database_and_server_key_cannot_reveal_api_key(app, client, tmp_path):
    """The operator's view: the whole database file plus ENCRYPTION_KEY. No Zanda key, no way to unlock one."""
    signup(client)
    add_site(client, key="zanda-live-key-ABCDEFGH")
    kiosk_signin(client)
    raw = (tmp_path / "t.db").read_bytes()
    for wal in tmp_path.glob("t.db-*"):
        raw += wal.read_bytes()
    assert b"zanda-live-key" not in raw and PASSWORD.encode() not in raw
    assert client.cookies.get("zc_kiosk").encode() not in raw  # device token stored only as a hash
    # With only the server key, the stored blob is still AES-GCM ciphertext under the practice key.
    from zanda_checkin.keyring import Keyring
    db = app.state.Session()
    site = db.get(Site, 1)
    inner = app.state.pepper.decrypt(site.zanda_key_encrypted)
    assert "zanda-live-key" not in inner
    for guess in (b"\0" * 32, app.state.settings.secret_key.encode()[:32].ljust(32, b"0")):
        with pytest.raises(ValueError):
            Keyring(app.state.pepper).decrypt_api_key(guess, site.zanda_key_encrypted, site.organisation_id, 1)


def test_kiosk_keeps_working_after_restart(app, client, tmp_path):
    """No key is held by the server between requests: a fresh process unlocks it from the iPad's token."""
    signup(client)
    add_site(client)
    kiosk_signin(client)
    cookie = client.cookies.get("zc_kiosk")
    app2 = create_app(app.state.settings, client_factory=DemoZandaClient)
    with TestClient(app2) as ipad:
        ipad.cookies.set("zc_kiosk", cookie, path="/checkin")
        assert ipad.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).json()["status"] == "match"


def test_wrong_server_key_unlocks_nothing(app, client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    cookie = client.cookies.get("zc_kiosk")
    s = app.state.settings
    bad = Settings(**{**s.__dict__, "encryption_key": Fernet.generate_key().decode()})
    with TestClient(create_app(bad, client_factory=DemoZandaClient)) as ipad:
        ipad.cookies.set("zc_kiosk", cookie, path="/checkin")
        assert ipad.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).status_code == 401


# -- HTTPS everywhere ----------------------------------------------------------------
def https_app(tmp_path, **kw):
    s = Settings.from_env(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'h.db'}", secret_key="s",
                          encryption_key=Fernet.generate_key().decode(), signup_code="invite",
                          public_url="https://checkin.example.com", demo=False, keys_from_env=True, **kw)
    return create_app(s, client_factory=DemoZandaClient)


def test_refuses_to_start_without_https(tmp_path):
    s = Settings.from_env(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'x.db'}", secret_key="s",
                          encryption_key=Fernet.generate_key().decode(), signup_code="", public_url="", demo=False)
    with pytest.raises(RuntimeError, match="https"):
        create_app(s)


@pytest.mark.parametrize("query", ["", "?sslmode=prefer", "?sslmode=require", "?sslmode=disable"])
def test_refuses_unverified_postgres(tmp_path, query):
    s = Settings.from_env(data_dir=tmp_path, database_url=f"postgresql+psycopg://u:p@db.example.com/zc{query}",
                          secret_key="s", encryption_key=Fernet.generate_key().decode(), signup_code="x",
                          public_url="https://checkin.example.com", demo=False, keys_from_env=True)
    with pytest.raises(RuntimeError, match="verify-full"):
        s.validate()


def test_accepts_verified_postgres(tmp_path):
    s = Settings.from_env(data_dir=tmp_path, database_url="postgresql+psycopg://u:p@db.example.com/zc?sslmode=verify-full",
                          secret_key="s", encryption_key=Fernet.generate_key().decode(), signup_code="x",
                          public_url="https://checkin.example.com", demo=False, keys_from_env=True)
    s.validate()


def test_production_needs_env_keys_and_signup_code(tmp_path):
    base = dict(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'p.db'}", secret_key="s",
                encryption_key=Fernet.generate_key().decode(), public_url="https://checkin.example.com", demo=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY and ENCRYPTION_KEY"):
        Settings.from_env(**base, signup_code="x", keys_from_env=False).validate()
    with pytest.raises(RuntimeError, match="SIGNUP_CODE"):
        Settings.from_env(**base, signup_code="", keys_from_env=True).validate()


def test_http_redirects_to_https_and_hsts(tmp_path):
    with TestClient(https_app(tmp_path), base_url="http://checkin.example.com") as c:
        r = c.get("/login", follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == "https://checkin.example.com/login"
        assert c.post("/api/v1/auth/login", json={}).status_code == 400
    with TestClient(https_app(tmp_path), base_url="https://checkin.example.com") as c:
        r = c.get("/login")
        assert "preload" in r.headers["strict-transport-security"]
        assert "upgrade-insecure-requests" in r.headers["content-security-policy"]
        assert r.headers["set-cookie"].startswith("__Host-zc_session=") and "secure" in r.headers["set-cookie"].lower()


def test_unknown_host_rejected(tmp_path):
    with TestClient(https_app(tmp_path), base_url="https://evil.example.net") as c:
        assert c.get("/login").status_code == 400


def test_security_headers(client):
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert "camera=()" in r.headers["permissions-policy"]


# -- fixes from the independent security review ----------------------------------------
def test_expired_session_key_copy_is_deleted_and_unusable(app, client):
    from datetime import datetime, timedelta, timezone
    from zanda_checkin.db import KeyWrap, WebSession
    signup(client)
    db = app.state.Session()
    ws = db.query(WebSession).one()
    ws.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/login"
    db.expire_all()
    assert db.query(KeyWrap).filter(KeyWrap.holder.like("session:%")).count() == 0


def test_session_key_copy_is_bound_to_expiry(app, client):
    """Pushing a session's expiry later in the database doesn't make its key copy usable."""
    from datetime import timedelta
    from zanda_checkin.db import WebSession
    signup(client)
    db = app.state.Session()
    ws = db.query(WebSession).one()
    ws.expires_at = ws.expires_at + timedelta(days=30)
    db.commit()
    assert client.get("/admin/sites/new").status_code == 200  # page itself doesn't need the key
    r = add_site(client)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_purge_removes_orphaned_and_expired_copies(app, client):
    from datetime import datetime, timedelta, timezone
    from zanda_checkin.db import KeyWrap, WebSession
    signup(client)
    db = app.state.Session()
    db.query(WebSession).update({"expires_at": datetime.now(timezone.utc) - timedelta(hours=1)})
    db.commit()
    assert app.state.accounts.purge_expired(db) == 1
    assert db.query(KeyWrap).filter(KeyWrap.holder.like("session:%")).count() == 0


def test_ipad_exit_failures_do_not_lock_out_staff(app, client):
    signup(client)
    add_site(client)
    ipad = TestClient(app)
    kiosk_signin(ipad)
    for _ in range(15):
        login(ipad, "/checkin/exit", password="wrong wrong wrong wrong", code="")
    logout(client)
    assert login(client, "/login").status_code == 303


def test_weak_password_rejected(client):
    token = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": token, "practice_name": "P", "email": "a@b.co",
                                     "password": "Password12345", "password2": "Password12345"})
    assert r.status_code == 400 and "harder-to-guess" in r.text


def test_signup_rate_limited(client):
    for _ in range(5):
        client.post("/signup", data={"csrf": csrf(client, "/signup"), "practice_name": "P", "email": "a@b.co",
                                     "password": "x", "password2": "y"})
    r = client.post("/signup", data={"csrf": csrf(client, "/signup"), "practice_name": "P", "email": "a@b.co",
                                     "password": "x", "password2": "y"})
    assert r.status_code == 429


def test_large_bodies_rejected(client):
    big = "x" * (70 * 1024)
    assert client.post("/checkin/api/lookup", content=big,
                       headers={"content-type": "application/json"}).status_code == 413

    def chunks():
        for _ in range(80):
            yield b"x" * 1024
    assert client.post("/api/v1/auth/login", content=chunks(),
                       headers={"content-type": "application/json"}).status_code == 413


def test_stream_cap_per_device(app):
    b = app.state.broadcaster
    assert all(b.claim(7) for _ in range(3)) and not b.claim(7)
    b.release(7)
    assert b.claim(7)


def test_names_can_be_left_out_of_notifications(app, client):
    signup(client)
    add_site(client)
    db = app.state.Session()
    db.get(Site, 1).notify_names = False
    db.commit()
    kiosk_signin(client)
    received = []

    async def go():
        q = app.state.broadcaster.subscribe(1)
        loop = asyncio.get_running_loop()

        def checkin():
            res = client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).json()
            client.post("/checkin/api/confirm", json={"token": res["token"]})
        await loop.run_in_executor(None, checkin)
        received.append(json.loads(await asyncio.wait_for(q.get(), 5)))

    asyncio.run(go())
    assert received[0]["message"] == "A client has arrived for Sam Rivera" and "Alex" not in json.dumps(received[0])


def test_kiosk_lockout_survives_settings_save(app, client):
    signup(client)
    add_site(client)
    kiosk_signin(client)
    for _ in range(6):
        client.post("/checkin/api/lookup", json={"dob": "09091999", "initial": "Q"})
    app.state.registry.forget(1)  # what saving the site's settings does
    assert client.post("/checkin/api/lookup", json={"dob": "01021990", "initial": "N"}).json()["status"] == "reception"


def test_totp_code_accepted_only_once_under_race(app, client):
    from zanda_checkin.db import User
    from conftest import SECRETS, FakeClock
    from zanda_checkin import mfa
    signup(client)
    db1, db2 = app.state.Session(), app.state.Session()
    u1 = db1.query(User).one()
    u2 = db2.query(User).one()  # both loaded before either uses the code
    code = mfa._code(SECRETS["owner@example.com"], int(FakeClock.tick() // 30))
    results = [app.state.accounts.check_code(db1, u1, code), app.state.accounts.check_code(db2, u2, code)]
    assert results.count(True) == 1
