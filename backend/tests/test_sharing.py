import pytest
from fastapi.testclient import TestClient

from quill import db
from quill.app import create_app
from quill.config import Settings

from test_api import seed


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data_dir=tmp_path, frontend_dist=tmp_path / "dist"))


@pytest.fixture
def owner(app):
    with TestClient(app) as c:
        assert c.post("/api/auth/setup", json={"email": "ada@x.io", "password": "ada password", "name": "Ada"}).status_code == 200
        yield c


def member(app, owner, email, name="") -> tuple[TestClient, int]:
    url = owner.post("/api/invites", json={"email": email, "name": name}).json()["url"]
    c = TestClient(app)
    r = c.post("/api/auth/accept", json={"token": url.rsplit("/", 1)[1], "password": "member pass 1"})
    assert r.status_code == 200, r.text
    return c, r.json()["user"]["id"]


@pytest.fixture
def team(app, owner):
    bob, bob_id = member(app, owner, "bob@x.io", "Bob")
    cat, cat_id = member(app, owner, "cat@x.io", "Cat")
    mid = seed(app.state.settings, owner_id=1)
    return {"bob": bob, "bob_id": bob_id, "cat": cat, "cat_id": cat_id, "mid": mid}


def share(client, mid, **body):
    r = client.patch(f"/api/meetings/{mid}/sharing", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def can_read(c, mid) -> bool:
    return c.get(f"/api/meetings/{mid}").status_code == 200


def test_private_until_shared(owner, team):
    bob, mid = team["bob"], team["mid"]
    assert bob.get("/api/meetings").json() == []
    assert not can_read(bob, mid)
    assert owner.get(f"/api/meetings/{mid}").json()["access"] == "owner"
    assert owner.get("/api/meetings").json()[0]["shared_with"] == 0


def test_share_with_a_person_view(owner, team):
    bob, cat, mid = team["bob"], team["cat"], team["mid"]
    state = share(owner, mid, people={str(team["bob_id"]): "view"})
    assert state["owner"]["name"] == "Ada" and [p["name"] for p in state["people"]] == ["Bob"]
    listed = bob.get("/api/meetings").json()
    assert [m["id"] for m in listed] == [mid]
    assert listed[0]["access"] == "view" and listed[0]["owner"] == {"id": 1, "name": "Ada", "email": "ada@x.io"}
    assert "shared_with" not in listed[0]
    # every read path works for a viewer
    for path in ("", "/transcript", "/notes", "/frames", "/moments", "/events", "/media", "/export?format=md"):
        assert bob.get(f"/api/meetings/{mid}{path}").status_code == 200, path
    frame_id = bob.get(f"/api/meetings/{mid}/frames").json()[0]["id"]
    assert bob.get(f"/api/frames/{frame_id}/image?thumb=1").status_code == 200
    assert bob.get("/api/search", params={"q": "budget"}).json()["transcript"][0]["meeting_id"] == mid
    assert bob.get("/api/events", params={"once": 1}).text.count('"id":"' + mid) == 1
    # but nothing that changes it
    assert bob.patch(f"/api/meetings/{mid}", json={"title": "Mine now"}).status_code == 403
    assert bob.patch(f"/api/meetings/{mid}/speakers/S1", json={"display_name": "X"}).status_code == 403
    assert bob.post(f"/api/meetings/{mid}/rerun", json={"stage": "synthesize"}).status_code == 403
    assert bob.delete(f"/api/meetings/{mid}").status_code == 403
    assert bob.get(f"/api/meetings/{mid}/sharing").status_code == 403
    # and Cat still sees nothing
    assert not can_read(cat, mid) and cat.get("/api/search", params={"q": "budget"}).json()["transcript"] == []
    assert owner.get("/api/meetings").json()[0]["shared_with"] == 1


def test_edit_access(owner, team):
    bob, mid = team["bob"], team["mid"]
    share(owner, mid, people={str(team["bob_id"]): "edit"})
    r = bob.patch(f"/api/meetings/{mid}", json={"title": "Renamed by Bob"})
    assert r.status_code == 200 and r.json()["access"] == "edit"
    assert bob.patch(f"/api/meetings/{mid}/speakers/S1", json={"display_name": "Alicia"}).status_code == 200
    assert bob.post(f"/api/meetings/{mid}/speakers/merge", json={"from": "S3", "into": "S2"}).status_code == 200
    assert bob.post(f"/api/meetings/{mid}/rerun", json={"stage": "synthesize"}).status_code == 403
    assert bob.delete(f"/api/meetings/{mid}").status_code == 403


def test_share_with_everyone_and_upgrade(owner, team):
    bob, cat, mid = team["bob"], team["cat"], team["mid"]
    share(owner, mid, everyone="view")
    assert can_read(bob, mid) and can_read(cat, mid)
    assert cat.get(f"/api/meetings/{mid}").json()["everyone_access"] == "view"
    # a personal share can raise one person above everyone's level
    share(owner, mid, people={str(team["cat_id"]): "edit"})
    assert cat.get(f"/api/meetings/{mid}").json()["access"] == "edit"
    assert bob.get(f"/api/meetings/{mid}").json()["access"] == "view"
    share(owner, mid, everyone=None)
    assert not can_read(bob, mid) and can_read(cat, mid)


def test_unshare(owner, team):
    bob, mid = team["bob"], team["mid"]
    share(owner, mid, people={str(team["bob_id"]): "view"})
    state = share(owner, mid, people={str(team["bob_id"]): None})
    assert state["people"] == [] and not can_read(bob, mid)


def test_sharing_validation(owner, team):
    mid = team["mid"]
    assert owner.patch(f"/api/meetings/{mid}/sharing", json={"people": {"1": "view"}}).status_code == 400
    assert owner.patch(f"/api/meetings/{mid}/sharing", json={"people": {"999": "view"}}).status_code == 400
    assert owner.patch(f"/api/meetings/{mid}/sharing", json={"people": {str(team["bob_id"]): "admin"}}).status_code == 400
    assert owner.patch(f"/api/meetings/{mid}/sharing", json={"everyone": "owner"}).status_code == 400
    assert owner.patch(f"/api/meetings/{mid}/sharing", json={"people": {"bob": "view"}}).status_code == 400
    owner.patch(f"/api/users/{team['cat_id']}", json={"disabled": True})
    r = owner.patch(f"/api/meetings/{mid}/sharing", json={"people": {str(team["cat_id"]): "view"}})
    assert r.status_code == 400 and "active" in r.json()["error"]
    assert owner.patch("/api/meetings/nope/sharing", json={}).status_code == 404


def test_directory_lists_active_people(owner, team):
    owner.patch(f"/api/users/{team['cat_id']}", json={"disabled": True})
    names = [p["name"] for p in team["bob"].get("/api/directory").json()]
    assert names == ["Ada", "Bob"]


def test_shares_go_away_with_the_meeting_and_the_person(app, owner, team):
    mid = team["mid"]
    share(owner, mid, people={str(team["bob_id"]): "view", str(team["cat_id"]): "edit"})
    owner.delete(f"/api/users/{team['cat_id']}")
    assert [p["name"] for p in owner.get(f"/api/meetings/{mid}/sharing").json()["people"]] == ["Bob"]
    owner.delete(f"/api/meetings/{mid}")
    with db.opened(app.state.settings.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM meeting_shares").fetchone()[0] == 0


def test_transfer_drops_the_new_owners_own_share(app, owner, team):
    bob = team["bob"]
    bob_mid = seed(app.state.settings, owner_id=team["bob_id"], title="Bob's")
    share(bob, bob_mid, people={"1": "view"})
    assert owner.get(f"/api/meetings/{bob_mid}").json()["access"] == "view"
    owner.delete(f"/api/users/{team['bob_id']}", params={"meetings": "transfer"})
    assert owner.get(f"/api/meetings/{bob_mid}").json()["access"] == "owner"
    assert owner.get(f"/api/meetings/{bob_mid}/sharing").json()["people"] == []
