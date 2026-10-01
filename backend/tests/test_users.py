import pytest
from fastapi.testclient import TestClient

from quill import auth, db, users
from quill.app import create_app
from quill.config import Settings

ADMIN = {"email": "admin@example.com", "password": "correct horse", "name": "Ada Admin"}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=tmp_path, public_url="https://quill.example"))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        assert c.post("/api/auth/setup", json=ADMIN).status_code == 200
        yield c


def other(app):
    return TestClient(app)


def token_of(url: str) -> str:
    return url.rsplit("/", 1)[1]


def invite(client, **body) -> dict:
    r = client.post("/api/invites", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def join(app, inv: dict, password="member pass 1", **extra) -> TestClient:
    c = other(app)
    r = c.post("/api/auth/accept", json={"token": token_of(inv["url"]), "password": password, **extra})
    assert r.status_code == 200, r.text
    return c


def people(client) -> dict[str, dict]:
    return {p["email"]: p for p in client.get("/api/users").json()}


# ------------------------------------------------------------ setup + me

def test_setup_stores_name_and_role(client):
    u = client.get("/api/auth/me").json()["user"]
    assert u["name"] == "Ada Admin" and u["role"] == "admin" and u["is_admin"]


# ------------------------------------------------------------ invites

def test_invite_preview_and_accept_with_name_and_role(app, client):
    inv = invite(client, email="Bob@Example.com", name="Bob", is_admin=True, days=1)
    assert inv["url"].startswith("https://quill.example/invite/")
    assert inv["status"] == "pending" and inv["role"] == "admin" and inv["invited_by"] == "Ada Admin"
    anon = other(app)
    p = anon.get("/api/auth/invite", params={"token": token_of(inv["url"])}).json()
    assert p == {**p, "email": "bob@example.com", "name": "Bob", "role": "admin", "invited_by": "Ada Admin"}
    bob = join(app, inv, name="  Bob   Builder ")
    me = bob.get("/api/auth/me").json()["user"]
    assert me["name"] == "Bob Builder" and me["is_admin"]
    assert people(client)["bob@example.com"]["invited_by"] == "Ada Admin"
    listed = client.get("/api/invites").json()[0]
    assert listed["status"] == "used" and listed["used_by"] == "bob@example.com"
    r = anon.get("/api/auth/invite", params={"token": token_of(inv["url"])})
    assert r.status_code == 410 and "already been used" in r.json()["error"]


def test_open_invite_needs_an_email(app, client):
    inv = invite(client)
    assert inv["email"] is None
    anon = other(app)
    assert anon.get("/api/auth/invite", params={"token": token_of(inv["url"])}).json()["email"] is None
    r = anon.post("/api/auth/accept", json={"token": token_of(inv["url"]), "password": "member pass 1"})
    assert r.status_code == 400
    r = anon.post("/api/auth/accept", json={"token": token_of(inv["url"]), "password": "member pass 1",
                                             "email": "Carol@Example.com", "name": "Carol"})
    assert r.status_code == 200 and r.json()["user"]["email"] == "carol@example.com"
    assert not r.json()["user"]["is_admin"]


def test_open_invite_cannot_take_an_existing_email(app, client):
    inv = invite(client)
    r = other(app).post("/api/auth/accept", json={"token": token_of(inv["url"]), "password": "member pass 1",
                                                  "email": "admin@example.com"})
    assert r.status_code == 409


def test_reinvite_withdraws_the_older_link(app, client):
    first = invite(client, email="dan@example.com")
    second = invite(client, email="dan@example.com")
    anon = other(app)
    r = anon.get("/api/auth/invite", params={"token": token_of(first["url"])})
    assert r.status_code == 410 and "withdrawn" in r.json()["error"] and "Ada Admin" in r.json()["error"]
    assert anon.get("/api/auth/invite", params={"token": token_of(second["url"])}).status_code == 200


def test_withdraw_and_renew(app, client):
    inv = invite(client, email="erin@example.com")
    assert client.delete(f"/api/invites/{inv['id']}").json()["status"] == "revoked"
    anon = other(app)
    r = anon.post("/api/auth/accept", json={"token": token_of(inv["url"]), "password": "member pass 1"})
    assert r.status_code == 403 and "withdrawn" in r.json()["error"]
    renewed = client.post(f"/api/invites/{inv['id']}/renew", json={"days": 30}).json()
    assert renewed["status"] == "pending" and renewed["id"] == inv["id"] and renewed["url"] != inv["url"]
    assert anon.get("/api/auth/invite", params={"token": token_of(inv["url"])}).status_code == 404
    join(app, renewed)
    assert client.post(f"/api/invites/{inv['id']}/renew").status_code == 409
    assert client.delete(f"/api/invites/{inv['id']}").status_code == 409


def test_expired_invite_explains_itself(app, client):
    inv = invite(client, email="fay@example.com")
    with db.opened(app.state.settings.db_path) as conn:
        conn.execute("UPDATE invites SET expires_at='2020-01-02T00:00:00+00:00' WHERE id=?", (inv["id"],))
    r = other(app).get("/api/auth/invite", params={"token": token_of(inv["url"])})
    assert r.status_code == 410 and "expired on 2 Jan 2020" in r.json()["error"]
    assert client.get("/api/invites").json()[0]["status"] == "expired"


def test_invite_validation(client):
    assert client.post("/api/invites", json={"email": "admin@example.com"}).status_code == 409
    assert client.post("/api/invites", json={"email": "not-an-email"}).status_code == 400
    assert client.post("/api/invites", json={"days": 3}).status_code == 400
    assert client.get("/api/auth/invite", params={"token": "nope"}).status_code == 404


# ------------------------------------------------------------ your account

def test_profile_update_and_email_change_needs_password(client):
    r = client.patch("/api/account", json={"name": "Ada L."})
    assert r.json()["user"]["name"] == "Ada L."
    r = client.patch("/api/account", json={"email": "ada@example.com", "current_password": "wrong one"})
    assert r.status_code == 400  # never 401: that would sign the browser out
    r = client.patch("/api/account", json={"email": "Ada@Example.com", "current_password": "correct horse"})
    assert r.json()["user"]["email"] == "ada@example.com"
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"email": "ada@example.com", "password": "correct horse"}).status_code == 200


def test_change_password_signs_out_other_devices(app, client):
    laptop = other(app)
    laptop.post("/api/auth/login", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    assert laptop.get("/api/auth/me").status_code == 200
    r = client.post("/api/account/password", json={"current_password": "nope nope", "new_password": "new password 9"})
    assert r.status_code == 400
    r = client.post("/api/account/password", json={"current_password": "correct horse", "new_password": "new password 9"})
    assert r.json() == {"ok": True, "signed_out": 1}
    assert client.get("/api/auth/me").status_code == 200
    assert laptop.get("/api/auth/me").status_code == 401


def test_sessions_list_and_revoke(app, client):
    phone = other(app)
    phone.headers["user-agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1"
    phone.post("/api/auth/login", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    sessions = client.get("/api/account/sessions").json()
    assert len(sessions) == 2 and sessions[0]["current"] and not sessions[1]["current"]
    assert "iPhone" in sessions[1]["user_agent"] and sessions[1]["created_at"]
    assert client.delete(f"/api/account/sessions/{sessions[1]['id']}").json()["current"] is False
    assert phone.get("/api/auth/me").status_code == 401
    phone.post("/api/auth/login", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    assert client.post("/api/account/sessions/revoke-others").json() == {"signed_out": 1}
    assert client.get("/api/auth/me").status_code == 200
    assert client.delete("/api/account/sessions/nope").status_code == 404


# ------------------------------------------------------------ people

def test_people_list_and_member_cannot_manage(app, client):
    bob = join(app, invite(client, email="bob@example.com"))
    ps = people(client)
    assert set(ps) == {"admin@example.com", "bob@example.com"}
    assert ps["bob@example.com"]["role"] == "member" and ps["bob@example.com"]["session_count"] == 1
    assert ps["admin@example.com"]["meeting_count"] == 0
    for method, path in [("GET", "/api/users"), ("PATCH", "/api/users/1"), ("POST", "/api/users/1/reset-link"),
                         ("DELETE", "/api/users/1"), ("GET", "/api/invites"), ("POST", "/api/invites")]:
        assert bob.request(method, path, json={}).status_code == 403, path


def test_turn_off_blocks_sign_in_and_kills_sessions(app, client):
    bob = join(app, invite(client, email="bob@example.com"))
    bob_id = people(client)["bob@example.com"]["id"]
    r = client.patch(f"/api/users/{bob_id}", json={"disabled": True})
    assert r.json()["disabled"] and r.json()["session_count"] == 0
    assert bob.get("/api/auth/me").status_code == 401
    r = bob.post("/api/auth/login", json={"email": "bob@example.com", "password": "member pass 1"})
    assert r.status_code == 403 and "turned off" in r.json()["error"]
    # a wrong password still says only "not correct"
    r = bob.post("/api/auth/login", json={"email": "bob@example.com", "password": "wrong pass 1"})
    assert r.status_code == 401
    client.patch(f"/api/users/{bob_id}", json={"disabled": False})
    assert bob.post("/api/auth/login", json={"email": "bob@example.com", "password": "member pass 1"}).status_code == 200


def test_last_admin_is_protected(app, client):
    me_id = client.get("/api/auth/me").json()["user"]["id"]
    assert client.patch(f"/api/users/{me_id}", json={"is_admin": False}).status_code == 409
    assert client.patch(f"/api/users/{me_id}", json={"disabled": True}).status_code == 400
    assert client.delete(f"/api/users/{me_id}").status_code == 400
    # with a second admin, stepping down works
    join(app, invite(client, email="bob@example.com", is_admin=True))
    r = client.patch(f"/api/users/{me_id}", json={"is_admin": False})
    assert r.status_code == 200 and r.json()["role"] == "member"
    assert client.get("/api/users").status_code == 403


def test_reset_link_flow(app, client):
    bob = join(app, invite(client, email="bob@example.com", name="Bob"))
    bob_id = people(client)["bob@example.com"]["id"]
    first = client.post(f"/api/users/{bob_id}/reset-link").json()
    link = client.post(f"/api/users/{bob_id}/reset-link").json()
    assert link["url"].startswith("https://quill.example/reset/") and link["email"] == "bob@example.com"
    anon = other(app)
    assert anon.get("/api/auth/reset", params={"token": token_of(first["url"])}).status_code == 404  # replaced
    p = anon.get("/api/auth/reset", params={"token": token_of(link["url"])}).json()
    assert p["email"] == "bob@example.com" and p["name"] == "Bob"
    assert anon.post("/api/auth/reset", json={"token": token_of(link["url"]), "password": "short"}).status_code == 400
    r = anon.post("/api/auth/reset", json={"token": token_of(link["url"]), "password": "brand new pass"})
    assert r.status_code == 200 and anon.get("/api/auth/me").status_code == 200
    assert bob.get("/api/auth/me").status_code == 401  # every other device signed out
    r = anon.post("/api/auth/reset", json={"token": token_of(link["url"]), "password": "brand new pass"})
    assert r.status_code == 410
    assert other(app).post("/api/auth/login", json={"email": "bob@example.com", "password": "brand new pass"}).status_code == 200


def test_sign_out_person(app, client):
    bob = join(app, invite(client, email="bob@example.com"))
    bob_id = people(client)["bob@example.com"]["id"]
    assert client.post(f"/api/users/{bob_id}/sign-out").json() == {"signed_out": 1}
    assert bob.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("action", ["transfer", "delete"])
def test_remove_person(app, client, action):
    join(app, invite(client, email="bob@example.com"))
    bob_id = people(client)["bob@example.com"]["id"]
    with db.opened(app.state.settings.db_path) as conn:
        mid = db.create_meeting(conn, owner_id=bob_id, title="Bob's standup", mode="audio", source_bytes=1000)
    assert people(client)["bob@example.com"]["meeting_count"] == 1
    r = client.delete(f"/api/users/{bob_id}", params={"meetings": action})
    assert r.json() == {"ok": True, "meetings": 1, "action": action}
    assert "bob@example.com" not in people(client)
    titles = [m["title"] for m in client.get("/api/meetings").json()]
    assert titles == (["Bob's standup"] if action == "transfer" else [])
    with db.opened(app.state.settings.db_path) as conn:
        left = conn.execute("SELECT count(*) FROM meetings WHERE id=?", (mid,)).fetchone()[0]
        assert left == (1 if action == "transfer" else 0)
        assert conn.execute("SELECT count(*) FROM sessions WHERE user_id=?", (bob_id,)).fetchone()[0] == 0
    assert client.delete(f"/api/users/{bob_id}").status_code == 404
    assert client.delete("/api/users/1", params={"meetings": "keep"}).status_code == 400


# ------------------------------------------------------------ last seen + migration + CLI

def test_last_seen_is_throttled(app, client):
    with db.opened(app.state.settings.db_path) as conn:
        conn.execute("UPDATE sessions SET last_seen_at='2020-01-01T00:00:00+00:00'")
    client.get("/api/auth/me")
    with db.opened(app.state.settings.db_path) as conn:
        seen = conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0]
    assert seen > "2026"
    with db.opened(app.state.settings.db_path) as conn:
        conn.execute("UPDATE sessions SET last_seen_at=?", (auth.iso_in(seconds=-10),))
        before = conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0]
    client.get("/api/auth/me")
    with db.opened(app.state.settings.db_path) as conn:
        assert conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0] == before


def test_v1_database_upgrades_with_invites_and_sessions(tmp_path, monkeypatch):
    path = tmp_path / "q.sqlite3"
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:1])
    db.init(path)
    with db.opened(path) as conn:
        conn.execute("INSERT INTO users(id, email, password_hash, is_admin, created_at) VALUES(1,'a@x.io','h',1,'t')")
        conn.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES('d1', 1, '2999-01-01')")
        conn.execute("INSERT INTO invites(token, email, created_by, expires_at) VALUES('d2', 'b@x.io', 1, '2999-01-01')")
    monkeypatch.undo()
    db.init(path)
    with db.opened(path) as conn:
        inv = conn.execute("SELECT * FROM invites").fetchone()
        assert inv["email"] == "b@x.io" and len(inv["id"]) == 16 and inv["revoked_at"] is None
        s = conn.execute("SELECT * FROM sessions").fetchone()
        assert len(s["id"]) == 16 and s["user_id"] == 1
        assert conn.execute("SELECT name, disabled_at FROM users").fetchone()[:] == (None, None)
        conn.execute("INSERT INTO invites(id, token, email, expires_at) VALUES('x', 'd3', NULL, '2999-01-01')")


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QUILL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("QUILL_PUBLIC_URL", "https://quill.example")
    settings = Settings.from_env()
    db.init(settings.db_path)
    with db.opened(settings.db_path) as conn:
        conn.execute("INSERT INTO users(email, password_hash, is_admin, created_at, disabled_at)"
                     " VALUES('a@x.io','h',1,'t',NULL), ('b@x.io','h',0,'t','t')")
    assert users.main(["list"]) == 0
    assert "a@x.io" in capsys.readouterr().out
    assert users.main(["reset-link", "A@x.io"]) == 0
    out = capsys.readouterr().out
    assert "https://quill.example/reset/" in out
    assert users.main(["set-role", "a@x.io", "member"]) == 1  # last admin
    assert users.main(["enable", "b@x.io"]) == 0
    assert users.main(["set-role", "b@x.io", "admin"]) == 0
    assert users.main(["set-role", "a@x.io", "member"]) == 0
    assert users.main(["reset-link", "nobody@x.io"]) == 1
