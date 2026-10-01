import base64
import hashlib
from collections import namedtuple
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quill import db, upload
from quill.app import create_app
from quill.config import Settings
from quill.stages import STAGES

TUS = {"Tus-Resumable": "1.0.0"}
OCTET = {**TUS, "Content-Type": "application/offset+octet-stream"}


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def meta(**kw) -> str:
    return ",".join(f"{k} {b64(str(v))}" if v != "" else k for k, v in kw.items())


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, max_upload_bytes=1000, disk_reserve_bytes=0)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        r = c.post("/api/auth/setup", json={"email": "admin@example.com", "password": "password1"})
        assert r.status_code == 200
        yield c


def create(client, length, **metadata):
    r = client.post("/api/uploads", headers={**TUS, "Upload-Length": str(length),
                                              "Upload-Metadata": meta(**metadata)})
    assert r.status_code == 201, r.text
    return r.headers["Location"]


def test_parse_metadata():
    assert upload.parse_metadata(f"filename {b64('a b.mp4')},audio_only") == {"filename": "a b.mp4", "audio_only": ""}
    with pytest.raises(upload.TusError):
        upload.parse_metadata("filename !!!notbase64")
    m = upload.clean_metadata({"filename": "../../etc/Team Sync.MKV", "expected_speakers": "3", "audio_only": "1",
                               "language": "auto"})
    assert m == {"filename": "Team Sync.MKV", "title": "Team Sync", "language": None, "expected_speakers": 3,
                 "audio_only": True}
    assert upload.source_extension("Team Sync.MKV") == "mkv"
    assert upload.source_extension("noext") == "bin"
    with pytest.raises(upload.TusError):
        upload.clean_metadata({"expected_speakers": "12"})


def test_options(client):
    r = client.options("/api/uploads")
    assert r.status_code == 204
    assert r.headers["Tus-Version"] == "1.0.0"
    assert "creation" in r.headers["Tus-Extension"] and "termination" in r.headers["Tus-Extension"]
    assert r.headers["Tus-Max-Size"] == "1000"
    assert "sha256" in r.headers["Tus-Checksum-Algorithm"]


def test_requires_auth_and_version(settings):
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/uploads", headers={**TUS, "Upload-Length": "10"}).status_code == 401
    # (a logged-in client without Tus-Resumable)
    with TestClient(create_app(settings)) as c:
        c.post("/api/auth/setup", json={"email": "a@example.com", "password": "password1"})
        r = c.post("/api/uploads", headers={"Upload-Length": "10"})
        assert r.status_code == 412 and r.headers["Tus-Version"] == "1.0.0"


def test_happy_path_with_resume(client, settings):
    data = bytes(range(256)) * 3  # 768 bytes
    loc = create(client, len(data), filename="Weekly sync.mp4", title="Weekly sync", language="en",
                 expected_speakers="4")
    h = client.head(loc, headers=TUS)
    assert h.status_code == 200 and h.headers["Upload-Offset"] == "0" and h.headers["Upload-Length"] == "768"
    assert h.headers["Cache-Control"] == "no-store"

    r = client.patch(loc, headers={**OCTET, "Upload-Offset": "0"}, content=data[:300])
    assert r.status_code == 204 and r.headers["Upload-Offset"] == "300"
    assert "Quill-Meeting-Id" not in r.headers
    # wrong offset -> 409
    r = client.patch(loc, headers={**OCTET, "Upload-Offset": "0"}, content=data[300:])
    assert r.status_code == 409
    # resume: HEAD tells where to continue
    off = int(client.head(loc, headers=TUS).headers["Upload-Offset"])
    digest = base64.b64encode(hashlib.sha256(data[off:]).digest()).decode()
    r = client.patch(loc, headers={**OCTET, "Upload-Offset": str(off), "Upload-Checksum": f"sha256 {digest}"},
                     content=data[off:])
    assert r.status_code == 204, r.text
    assert r.headers["Upload-Offset"] == "768"
    mid = r.headers["Quill-Meeting-Id"]

    src = settings.media_dir(mid) / "source.mp4"
    assert src.read_bytes() == data
    assert not (settings.uploads_dir / loc.rsplit("/", 1)[1]).exists()
    with db.opened(settings.db_path) as c:
        m = c.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
        assert (m["status"], m["title"], m["language"], m["expected_speakers"], m["mode"]) == \
            ("queued", "Weekly sync", "en", 4, "video")
        assert m["source_bytes"] == 768 and m["source_path"] == str(src) and m["source_name"] == "Weekly sync.mp4"
        stages = c.execute("SELECT name, status FROM stages WHERE meeting_id=?", (mid,)).fetchall()
        assert len(stages) == len(STAGES) and {s["status"] for s in stages} == {"pending"}
    # HEAD after completion still reports the meeting
    h = client.head(loc, headers=TUS)
    assert h.headers["Upload-Offset"] == "768" and h.headers["Quill-Meeting-Id"] == mid
    # meeting visible via API
    assert client.get(f"/api/meetings/{mid}").json()["status"] == "queued"


def test_creation_with_upload_audio_only(client, settings):
    data = b"x" * 50
    r = client.post("/api/uploads", headers={**OCTET, "Upload-Length": "50",
                                              "Upload-Metadata": meta(filename="call.m4a", audio_only="true")},
                    content=data)
    assert r.status_code == 201 and r.headers["Upload-Offset"] == "50"
    mid = r.headers["Quill-Meeting-Id"]
    with db.opened(settings.db_path) as c:
        m = c.execute("SELECT mode, title FROM meetings WHERE id=?", (mid,)).fetchone()
    assert m["mode"] == "audio" and m["title"] == "call"


def test_checksum_mismatch_discards_chunk(client):
    loc = create(client, 10, filename="a.mp3")
    bad = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
    r = client.patch(loc, headers={**OCTET, "Upload-Offset": "0", "Upload-Checksum": f"sha256 {bad}"},
                     content=b"0123456789")
    assert r.status_code == 460
    assert client.head(loc, headers=TUS).headers["Upload-Offset"] == "0"
    r = client.patch(loc, headers={**OCTET, "Upload-Offset": "0", "Upload-Checksum": "crc32 AAAA"}, content=b"01")
    assert r.status_code == 400


def test_limits(client, settings, monkeypatch):
    r = client.post("/api/uploads", headers={**TUS, "Upload-Length": "1001"})
    assert r.status_code == 413
    assert client.post("/api/uploads", headers={**TUS}).status_code == 400
    assert client.post("/api/uploads", headers={**TUS, "Upload-Defer-Length": "1"}).status_code == 400
    # body larger than declared length
    loc = create(client, 5, filename="a.wav")
    r = client.patch(loc, headers={**OCTET, "Upload-Offset": "0"}, content=b"0123456789")
    assert r.status_code == 413
    assert client.head(loc, headers=TUS).headers["Upload-Offset"] == "0"
    # wrong content type
    r = client.patch(loc, headers={**TUS, "Upload-Offset": "0", "Content-Type": "text/plain"}, content=b"01")
    assert r.status_code == 415


def test_disk_reservation(client, settings, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(upload.shutil, "disk_usage", lambda p: Usage(10_000, 0, 900))
    r = client.post("/api/uploads", headers={**TUS, "Upload-Length": "800"})
    assert r.status_code == 201
    # 800 bytes are now reserved by the unfinished upload: 900 - 800 < 200
    r = client.post("/api/uploads", headers={**TUS, "Upload-Length": "200"})
    assert r.status_code == 507
    object.__setattr__(settings, "disk_reserve_bytes", 50)  # frozen dataclass, test-only poke
    r = client.post("/api/uploads", headers={**TUS, "Upload-Length": "60"})
    assert r.status_code == 507
    r = client.post("/api/uploads", headers={**TUS, "Upload-Length": "40"})
    assert r.status_code == 201


def test_termination(client, settings):
    loc = create(client, 100, filename="a.mp4")
    client.patch(loc, headers={**OCTET, "Upload-Offset": "0"}, content=b"abc")
    uid = loc.rsplit("/", 1)[1]
    assert (settings.uploads_dir / uid).exists()
    assert client.delete(loc, headers=TUS).status_code == 204
    assert not (settings.uploads_dir / uid).exists()
    assert client.head(loc, headers=TUS).status_code == 404


def test_per_user_ownership(client, settings):
    loc = create(client, 10, filename="a.mp4")
    inv = client.post("/api/invites", json={"email": "bob@example.com"}).json()["url"]
    with TestClient(client.app) as bob:
        r = bob.post("/api/auth/accept", json={"token": inv.rsplit("/", 1)[1], "password": "bobs password"})
        assert r.status_code == 200
        assert bob.head(loc, headers=TUS).status_code == 404
        assert bob.patch(loc, headers={**OCTET, "Upload-Offset": "0"}, content=b"x").status_code == 404
        assert bob.delete(loc, headers=TUS).status_code == 404


def test_sweep_stale_uploads(client, settings):
    loc = create(client, 10, filename="a.mp4")
    with db.opened(settings.db_path) as c:
        c.execute("UPDATE uploads SET created_at='2000-01-01T00:00:00+00:00'")
    assert upload.sweep_stale_uploads(settings) == 1
    assert client.head(loc, headers=TUS).status_code == 404
