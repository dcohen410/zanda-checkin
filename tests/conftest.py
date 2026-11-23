import re

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from zanda_checkin import security
from zanda_checkin.app import create_app
from zanda_checkin.demo import DemoZandaClient
from zanda_checkin.settings import Settings

security._N = 2 ** 10  # fast password hashing in tests only


@pytest.fixture
def app(tmp_path):
    settings = Settings.from_env(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 't.db'}",
                                 secret_key="test-secret", encryption_key=Fernet.generate_key().decode(),
                                 signup_code="", public_url="", demo=False)
    return create_app(settings, client_factory=DemoZandaClient)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def csrf(client, url):
    html = client.get(url).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def signup(client, email="owner@example.com", password="correct horse battery", practice="Test Practice"):
    token = csrf(client, "/signup")
    return client.post("/signup", data={"csrf": token, "practice_name": practice, "name": "Owner",
                                        "email": email, "password": password, "password2": password},
                       follow_redirects=False)


def add_site(client, name="Main", key="good-key"):
    token = csrf(client, "/admin/sites/new")
    return client.post("/admin/sites/new", data={"csrf": token, "name": name, "region": "au", "api_key": key},
                       follow_redirects=False)


def kiosk_signin(client, email="owner@example.com", password="correct horse battery", site_id=1):
    token = csrf(client, "/checkin/login")
    client.post("/checkin/login", data={"csrf": token, "email": email, "password": password})
    token = csrf(client, "/checkin/choose-site")
    return client.post("/checkin/choose-site", data={"csrf": token, "site_id": site_id, "device_name": "iPad"},
                       follow_redirects=False)
