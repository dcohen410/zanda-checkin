import re

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from zanda_checkin import keyring, mfa, security
from zanda_checkin.app import create_app
from zanda_checkin.demo import DemoZandaClient
from zanda_checkin.settings import Settings

security._N = 2 ** 10  # fast password hashing in tests only
keyring.KDF_N = 2 ** 10

PASSWORD = "violet anchor maple tundra 42"


@pytest.fixture
def app(tmp_path):
    settings = Settings.from_env(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 't.db'}",
                                 secret_key="test-secret", encryption_key=Fernet.generate_key().decode(),
                                 signup_code="", public_url="", demo=False, allow_insecure_http=True)
    return create_app(settings, client_factory=DemoZandaClient)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def csrf(client, url):
    html = client.get(url).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class FakeClock:
    """Each new code moves time on by one 30-second step, since a code can't be reused."""
    now = 1_900_000_000.0

    @classmethod
    def time(cls):
        return cls.now

    @classmethod
    def tick(cls):
        cls.now += 30
        return cls.now


mfa.time = FakeClock  # mfa.verify reads mfa.time.time()
SECRETS: dict[str, str] = {}


def next_code(email="owner@example.com"):
    return mfa._code(SECRETS[email], int(FakeClock.tick() // 30))


def signup(client, email="owner@example.com", password=PASSWORD, practice="Test Practice", enrol=True):
    token = csrf(client, "/signup")
    r = client.post("/signup", data={"csrf": token, "practice_name": practice, "name": "Owner",
                                     "email": email, "password": password, "password2": password},
                    follow_redirects=False)
    if enrol and r.status_code == 303:
        page = client.get("/admin/mfa").text
        secret = re.search(r"<code>([A-Z2-7]{16,})</code>", page).group(1)
        SECRETS[email] = secret
        code = next_code(email)
        client.post("/admin/mfa", data={"csrf": csrf(client, "/admin/mfa"), "code": code})
    return r


def login(client, url, email="owner@example.com", password=PASSWORD, code=None, **extra):
    token = csrf(client, url)
    data = {"csrf": token, "email": email, "password": password,
            "code": code if code is not None else mfa_code_for(client.app, email), **extra}
    return client.post(url, data=data, follow_redirects=False)


def mfa_code_for(app, email):
    return next_code(email)


def add_site(client, name="Main", key="good-key"):
    token = csrf(client, "/admin/sites/new")
    return client.post("/admin/sites/new", data={"csrf": token, "name": name, "region": "au", "api_key": key},
                       follow_redirects=False)


def kiosk_signin(client, email="owner@example.com", password=PASSWORD, site_id=1):
    login(client, "/checkin/login", email, password)
    token = csrf(client, "/checkin/choose-site")
    return client.post("/checkin/choose-site", data={"csrf": token, "site_id": site_id, "device_name": "iPad"},
                       follow_redirects=False)


def api_login(client, email="owner@example.com", password=PASSWORD):
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password,
                                                "code": mfa_code_for(client.app, email), "deviceName": "Mac"})
    assert r.status_code == 200, r.text
    return r.json()["token"]
