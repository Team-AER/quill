import pytest
from fastapi.testclient import TestClient

from quill import auth
from quill.app import create_app
from quill.config import Settings


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as c:
        yield c


def setup_admin(client, email="admin@example.com", password="correct horse"):
    r = client.post("/api/auth/setup", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["user"]


def test_password_hashing():
    h = auth.hash_password("s3cret-pass")
    assert h.startswith("scrypt$") and auth.verify_password("s3cret-pass", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")


def test_first_run_setup_flow(client):
    assert client.get("/api/auth/state").json() == {"needs_setup": True}
    assert client.get("/api/auth/me").status_code == 401
    user = setup_admin(client)
    assert user["is_admin"] and user["email"] == "admin@example.com"
    assert client.get("/api/auth/state").json() == {"needs_setup": False}
    assert client.get("/api/auth/me").json()["user"]["email"] == "admin@example.com"
    # second setup refused
    r = client.post("/api/auth/setup", json={"email": "b@example.com", "password": "another pass"})
    assert r.status_code == 409 and "error" in r.json()


def test_cookie_flags(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, public_url="https://quill.example.com"))
    with TestClient(app, base_url="https://testserver") as c:
        r = c.post("/api/auth/setup", json={"email": "a@example.com", "password": "password1"})
        cookie = r.headers["set-cookie"].lower()
        assert "quill_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie


def test_cookie_not_secure_on_http(client):
    r = client.post("/api/auth/setup", json={"email": "a@example.com", "password": "password1"})
    assert "secure" not in r.headers["set-cookie"].lower()


def test_short_password_rejected(client):
    r = client.post("/api/auth/setup", json={"email": "a@example.com", "password": "short"})
    assert r.status_code == 400


def test_login_logout(client):
    setup_admin(client)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/login", json={"email": "ADMIN@example.com ", "password": "wrong password"})
    assert r.status_code == 401 and r.json()["error"]
    r = client.post("/api/auth/login", json={"email": "ADMIN@example.com ", "password": "correct horse"})
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_login_rate_limit(client):
    setup_admin(client)
    client.post("/api/auth/logout")
    codes = [client.post("/api/auth/login", json={"email": "admin@example.com", "password": "nope nope"}).status_code
             for _ in range(auth.LOGIN_PER_EMAIL + 1)]
    assert codes[:auth.LOGIN_PER_EMAIL] == [401] * auth.LOGIN_PER_EMAIL
    assert codes[-1] == 429
    # even the right password is throttled now
    r = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "correct horse"})
    assert r.status_code == 429


def test_invite_flow(client):
    setup_admin(client)
    r = client.post("/api/invites", json={"email": "Bob@Example.com"})
    assert r.status_code == 200
    url = r.json()["url"]
    assert "/invite/" in url
    token = url.rsplit("/", 1)[1]
    assert client.get("/api/invites").json()[0]["email"] == "bob@example.com"
    client.post("/api/auth/logout")

    bad = client.post("/api/auth/accept", json={"token": "nope", "password": "bobs password"})
    assert bad.status_code == 403
    r = client.post("/api/auth/accept", json={"token": token, "password": "bobs password"})
    assert r.status_code == 200 and r.json()["user"] == {**r.json()["user"], "email": "bob@example.com", "is_admin": False}
    assert client.get("/api/auth/me").json()["user"]["email"] == "bob@example.com"
    # token single use
    client.post("/api/auth/logout")
    assert client.post("/api/auth/accept", json={"token": token, "password": "bobs password"}).status_code == 403
    # non-admins cannot invite
    client.post("/api/auth/login", json={"email": "bob@example.com", "password": "bobs password"})
    assert client.post("/api/invites", json={"email": "c@example.com"}).status_code == 403


def test_invite_requires_auth(client):
    setup_admin(client)
    client.post("/api/auth/logout")
    assert client.post("/api/invites", json={"email": "c@example.com"}).status_code == 401


def test_invite_existing_email(client):
    setup_admin(client)
    assert client.post("/api/invites", json={"email": "admin@example.com"}).status_code == 409


def test_secret_key_changes_digest():
    a = auth.token_digest(Settings(), "tok")
    b = auth.token_digest(Settings(secret_key="k"), "tok")
    assert a != b


@pytest.mark.parametrize("ip,allowed", [("10.20.30.40", True), ("192.168.1.5", True),
                                        ("127.0.0.1", True), ("1.1.1.1", False),
                                        ("8.8.8.8", False)])
def test_setup_only_from_local_network(tmp_path, ip, allowed):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, client=(ip, 50000)) as c:
        r = c.post("/api/auth/setup", json={"email": "admin@example.com", "password": "correct horse"})
    assert (r.status_code == 200) is allowed, r.text
    if not allowed:
        assert r.status_code == 403


def test_cookie_not_secure_on_lan_http_even_with_https_public_url(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, public_url="https://quill.example.com"))
    with TestClient(app, base_url="http://192.168.1.73") as c:
        r = c.post("/api/auth/setup", json={"email": "a@example.com", "password": "password1"})
        assert "secure" not in r.headers["set-cookie"].lower()
        assert c.get("/api/auth/me").status_code == 200
