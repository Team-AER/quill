import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

import quill.pipeline
from quill import db
from quill.app import create_app
from quill.config import Settings

NOTES = {
    "tldr": ["Ship v1 on Friday"],
    "summary": "The team agreed to **ship**.",
    "chapters": [{"title": "Intro", "start": 0.0, "end": 5.0, "summary": "Hello"}],
    "decisions": [{"text": "Ship Friday", "t": 3.0, "grounded": True}],
    "action_items": [{"owner": "S2", "task": "Write release notes", "due": None, "t": 4.0, "grounded": True},
                     {"owner": None, "task": "Book room", "due": "2026-10-02", "t": 5.0, "grounded": False}],
    "open_questions": [{"text": "Budget?", "t": 6.0, "grounded": True}],
    "key_visuals": [{"frame_id": 1, "t": 2.0, "caption": "Roadmap slide"}],
    "speaker_suggestions": [{"label": "S2", "name": "Bob", "evidence_t": 1.0}],
}


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", frontend_dist=tmp_path / "dist")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/auth/setup", json={"email": "admin@example.com", "password": "password1"}).status_code == 200
        yield c


def seed(settings, owner_id=1, title="Weekly sync", mode="video"):
    with db.opened(settings.db_path) as c:
        mid = db.create_meeting(c, owner_id=owner_id, title=title, mode=mode, source_name="sync.mp4")
        media = settings.media_dir(mid)
        media.mkdir(parents=True)
        src = media / "source.mp4"
        src.write_bytes(bytes(range(256)) * 4)
        (media / "stt.opus").write_bytes(b"OggS" + b"\0" * 96)
        (media / "frames").mkdir()
        (media / "frames" / "f1.jpg").write_bytes(b"\xff\xd8full")
        (media / "frames" / "f1_t.jpg").write_bytes(b"\xff\xd8thumb")
        c.execute("UPDATE meetings SET source_path=?, has_video=1, duration_s=12.5, status='done' WHERE id=?",
                  (str(src), mid))
        c.execute("UPDATE stages SET status='done', progress=1 WHERE meeting_id=?", (mid,))
        c.executemany("INSERT INTO turns(meeting_id, speaker, start, end, overlap) VALUES(?,?,?,?,?)",
                      [(mid, "S1", 0.0, 2.5, 0), (mid, "S2", 2.5, 6.0, 0), (mid, "S3", 6.0, 7.0, 1)])
        cur = c.execute("INSERT INTO segments(meeting_id, idx, speaker, start, end, status, text, interjections)"
                        " VALUES(?,?,?,?,?,?,?,?)", (mid, 0, "S2", 2.5, 6.0, "done", "x",
                                                     json.dumps([{"speaker": "S3", "start": 4.0, "end": 4.5}])))
        seg = cur.lastrowid
        c.executemany("INSERT INTO transcript_lines(meeting_id, speaker, start, end, text, overlap, segment_id)"
                      " VALUES(?,?,?,?,?,?,?)",
                      [(mid, "S1", 0.0, 2.5, "Welcome to the quarterly budget review.", 0, None),
                       (mid, "S2", 2.5, 6.0, "We ship on Friday.", 0, seg),
                       (mid, "S3", 6.0, 7.0, "Agreed.", 1, None)])
        c.execute("INSERT INTO speakers(meeting_id, label, display_name, suggested_name, suggestion_evidence_t, color)"
                  " VALUES(?,?,?,?,?,?)", (mid, "S2", None, "Bob", 1.0, 1))
        c.execute("INSERT INTO speakers(meeting_id, label, display_name, color) VALUES(?,?,?,?)", (mid, "S1", "Alice", 0))
        cur = c.execute("INSERT INTO moments(meeting_id, t, source, why, look_for) VALUES(?,?,?,?,?)",
                        (mid, 2.0, "text", "slide shown", "roadmap"))
        c.execute("INSERT INTO frames(meeting_id, moment_id, t, path, thumb_path, kind, title, visible_text,"
                  " key_facts, relevance, caption) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (mid, cur.lastrowid, 2.0, str(media / "frames" / "f1.jpg"), "frames/f1_t.jpg", "slide", "Roadmap",
                   "Q4 Roadmap Milestones", json.dumps(["Launch Friday"]), 3, "Roadmap slide"))
        c.execute("INSERT INTO notes(meeting_id, version, json, model, created_at) VALUES(?,?,?,?,?)",
                  (mid, 1, json.dumps(NOTES), "gemma", "2026-09-30T00:00:00+00:00"))
    return mid


def test_list_and_detail(client, settings):
    mid = seed(settings)
    lst = client.get("/api/meetings").json()
    assert isinstance(lst, list) and lst[0]["id"] == mid
    assert lst[0]["speaker_count"] == 3 and lst[0]["progress"] == 1.0 and lst[0]["has_video"] is True
    assert lst[0]["cover_url"].endswith("/image?thumb=1") and lst[0]["gist"] == "Ship v1 on Friday"
    assert lst[0]["action_count"] == 2 and [s["name"] for s in lst[0]["speakers"]] == ["Alice", "Speaker 2", "Speaker 3"]
    d = client.get(f"/api/meetings/{mid}").json()
    assert [s["name"] for s in d["stages"]] == ["probe", "extract_audio", "diarize", "transcribe",
                                                 "key_moments", "frames", "synthesize"]
    assert set(d["stages"][0]) == {"name", "status", "progress", "detail", "started_at", "finished_at", "error"}
    sp = {s["label"]: s for s in d["speakers"]}
    assert sp["S1"]["name"] == "Alice" and sp["S2"]["name"] == "Speaker 2" and sp["S2"]["suggested_name"] == "Bob"
    assert sp["S3"]["display_name"] is None and sp["S2"]["talk_time_s"] == 3.5
    assert d["has_notes"] is True
    assert client.get("/api/meetings/nope").status_code == 404


def test_patch_title(client, settings):
    mid = seed(settings)
    assert client.patch(f"/api/meetings/{mid}", json={"title": "  Renamed "}).json()["title"] == "Renamed"
    assert client.patch(f"/api/meetings/{mid}", json={"title": ""}).status_code == 400


def test_transcript_notes_frames_moments(client, settings):
    mid = seed(settings)
    lines = client.get(f"/api/meetings/{mid}/transcript").json()
    assert [l["speaker"] for l in lines] == ["S1", "S2", "S3"]
    assert lines[1]["interjections"] == [{"speaker": "S3", "start": 4.0, "end": 4.5}]
    assert lines[2]["overlap"] is True
    notes = client.get(f"/api/meetings/{mid}/notes").json()
    assert notes["notes"]["tldr"] == ["Ship v1 on Friday"] and notes["version"] == 1
    frames = client.get(f"/api/meetings/{mid}/frames").json()
    assert frames[0]["key_facts"] == ["Launch Friday"] and frames[0]["thumb_url"].endswith("thumb=1")
    assert client.get(f"/api/meetings/{mid}/moments").json()[0]["look_for"] == "roadmap"
    img = client.get(frames[0]["image_url"])
    assert img.status_code == 200 and img.content == b"\xff\xd8full"
    assert client.get(frames[0]["thumb_url"]).content == b"\xff\xd8thumb"


def test_notes_empty(client, settings):
    with db.opened(settings.db_path) as c:
        mid = db.create_meeting(c, owner_id=1, title="Empty")
    assert client.get(f"/api/meetings/{mid}/notes").json()["notes"] is None


def test_speaker_rename_and_merge(client, settings):
    mid = seed(settings)
    r = client.patch(f"/api/meetings/{mid}/speakers/S2", json={"display_name": "Bob"})
    assert {s["label"]: s["name"] for s in r.json()}["S2"] == "Bob"
    assert client.patch(f"/api/meetings/{mid}/speakers/S9", json={"display_name": "X"}).status_code == 404
    # S3 only exists in turns: renaming creates the row
    client.patch(f"/api/meetings/{mid}/speakers/S3", json={"display_name": "Carol"})
    r = client.post(f"/api/meetings/{mid}/speakers/merge", json={"from": "S2", "into": "S3"})
    assert r.status_code == 200
    labels = [s["label"] for s in r.json()]
    assert labels == ["S1", "S3"]
    lines = client.get(f"/api/meetings/{mid}/transcript").json()
    assert [l["speaker"] for l in lines] == ["S1", "S3", "S3"]
    assert lines[1]["interjections"][0]["speaker"] == "S3"
    with db.opened(settings.db_path) as c:
        assert c.execute("SELECT count(*) FROM turns WHERE speaker='S2'").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM segments WHERE speaker='S2'").fetchone()[0] == 0
        notes = json.loads(c.execute("SELECT json FROM notes").fetchone()[0])
    assert notes["action_items"][0]["owner"] == "S3" and notes["speaker_suggestions"] == []
    assert client.post(f"/api/meetings/{mid}/speakers/merge", json={"from": "S1", "into": "S1"}).status_code == 400


def test_rerun(client, settings):
    mid = seed(settings)
    r = client.post(f"/api/meetings/{mid}/rerun", json={"stage": "diarize", "options": {"expected_speakers": 3}})
    assert r.status_code == 200
    d = r.json()
    st = {s["name"]: s["status"] for s in d["stages"]}
    assert st["probe"] == "done" and st["extract_audio"] == "done"
    assert all(st[n] == "pending" for n in ("diarize", "transcribe", "key_moments", "frames", "synthesize"))
    assert d["status"] == "queued" and d["expected_speakers"] == 3
    with db.opened(settings.db_path) as c:
        assert json.loads(c.execute("SELECT options FROM stages WHERE meeting_id=? AND name='diarize'",
                                    (mid,)).fetchone()[0]) == {"expected_speakers": 3}
    assert client.post(f"/api/meetings/{mid}/rerun", json={"stage": "bogus"}).status_code == 400


def test_rerun_video_stage_in_audio_mode(client, settings):
    mid = seed(settings, mode="audio")
    assert client.post(f"/api/meetings/{mid}/rerun", json={"stage": "frames"}).status_code == 400


def test_media_ranges(client, settings):
    mid = seed(settings)
    full = client.get(f"/api/meetings/{mid}/media")
    assert full.status_code == 200 and len(full.content) == 1024 and full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-type"] == "video/mp4"
    r = client.get(f"/api/meetings/{mid}/media", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and r.content == bytes(range(10, 20))
    assert r.headers["content-range"] == "bytes 10-19/1024" and r.headers["content-length"] == "10"
    r = client.get(f"/api/meetings/{mid}/media", headers={"Range": "bytes=1000-"})
    assert r.status_code == 206 and len(r.content) == 24
    r = client.get(f"/api/meetings/{mid}/media", headers={"Range": "bytes=-4"})
    assert r.content == bytes([252, 253, 254, 255])
    assert client.get(f"/api/meetings/{mid}/media", headers={"Range": "bytes=5000-"}).status_code == 416
    # after retention the audio proxy is served
    with db.opened(settings.db_path) as c:
        c.execute("UPDATE meetings SET source_deleted_at='2026-09-30' WHERE id=?", (mid,))
    r = client.get(f"/api/meetings/{mid}/media")
    assert r.status_code == 200 and r.content.startswith(b"OggS") and r.headers["content-type"] == "audio/ogg"


def test_exports(client, settings):
    mid = seed(settings)
    client.patch(f"/api/meetings/{mid}/speakers/S2", json={"display_name": "Bob"})
    txt = client.get(f"/api/meetings/{mid}/export?format=txt")
    assert txt.status_code == 200 and "attachment" in txt.headers["content-disposition"]
    assert txt.text.splitlines()[0] == "[00:00:00] Alice: Welcome to the quarterly budget review."
    assert "Bob: We ship on Friday." in txt.text and "Speaker 3: Agreed." in txt.text
    srt = client.get(f"/api/meetings/{mid}/export?format=srt").text
    assert srt.startswith("1\n00:00:00,000 --> 00:00:02,500\nAlice: Welcome")
    vtt = client.get(f"/api/meetings/{mid}/export?format=vtt").text
    assert vtt.startswith("WEBVTT\n") and "00:00:02.500 --> 00:00:06.000\n<v Bob>We ship on Friday." in vtt
    rttm = client.get(f"/api/meetings/{mid}/export?format=rttm").text.splitlines()
    assert rttm[1] == f"SPEAKER {mid} 1 2.500 3.500 <NA> <NA> S2 <NA> <NA>"
    js = client.get(f"/api/meetings/{mid}/export?format=json").json()
    assert js["notes"]["tldr"] and len(js["transcript"]) == 3 and js["meeting"]["id"] == mid
    md = client.get(f"/api/meetings/{mid}/export?format=md").text
    assert md.startswith("# Weekly sync")
    for part in ("## TL;DR", "- Ship v1 on Friday", "## Action items", "**Bob**: Write release notes",
                 "**Unassigned**: Book room (due 2026-10-02)", "_(unverified)_", "## Transcript", "[00:00:02]",
                 "## Chapters", "## Decisions", "## Open questions", "## Key visuals"):
        assert part in md, part
    assert client.get(f"/api/meetings/{mid}/export?format=pdf").status_code == 400


def test_search(client, settings):
    mid = seed(settings)
    r = client.get("/api/search", params={"q": "budget"}).json()
    assert r["transcript"][0]["meeting_id"] == mid and "[[budget]]" in r["transcript"][0]["snippet"]
    assert r["transcript"][0]["speaker_name"] == "Alice"
    r = client.get("/api/search", params={"q": "roadm"}).json()
    assert r["frames"][0]["caption"] == "Roadmap slide"
    assert client.get("/api/search", params={"q": "\"'*"}).json() == {"transcript": [], "frames": []}
    assert client.get("/api/search", params={"q": "zebra"}).json()["transcript"] == []


def test_sse_once(client, settings):
    mid = seed(settings)
    r = client.get("/api/events?once=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [b for b in r.text.split("\n\n") if b.startswith("event: meeting")]
    payload = json.loads(events[0].split("data: ", 1)[1])
    assert payload["id"] == mid and payload["status"] == "done" and len(payload["stages"]) == 7


def test_sse_pushes_changes(settings):
    from quill.api import meeting_events_snapshot
    settings.ensure_dirs()
    db.init(settings.db_path)
    mid = seed(settings)
    snap1 = meeting_events_snapshot(settings, 1)
    with db.opened(settings.db_path) as c:
        c.execute("UPDATE stages SET status='running', progress=0.5 WHERE meeting_id=? AND name='synthesize'", (mid,))
    snap2 = meeting_events_snapshot(settings, 1)
    assert snap1[mid] != snap2[mid] and snap2[mid]["current_stage"] == "synthesize"


def fake_catalog(monkeypatch, statuses):
    mod = types.ModuleType("quill.pipeline.catalog")
    mod.model_statuses = lambda settings: statuses
    monkeypatch.setitem(sys.modules, "quill.pipeline.catalog", mod)
    monkeypatch.setattr(quill.pipeline, "catalog", mod, raising=False)


def test_system_without_catalog(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "quill.pipeline.catalog", None)  # import fails
    monkeypatch.delattr(quill.pipeline, "catalog", raising=False)
    r = client.get("/api/system").json()
    assert r["stt"] == {"model": "aer-stt-v1", "status": "unknown"}
    assert r["disk"]["free_bytes"] > 0 and "worker" in r


def test_system_with_catalog(client, monkeypatch):
    fake_catalog(monkeypatch, {"aer-stt-v1": "disabled", "google/gemma-4-12B-it-qat-w4a16-ct": {"status": "ready"}})
    r = client.get("/api/system").json()
    assert r["stt"]["status"] == "disabled" and r["vision"]["status"] == "ready" and r["text"]["status"] == "ready"


def test_settings(client, settings, monkeypatch):
    fake_catalog(monkeypatch, {"aer-stt-v1": "ready", "aer-stt-qwen3": "disabled"})
    s = client.get("/api/settings").json()
    assert s["stt_model"] == "aer-stt-v1" and s["video_retention_days"] == 14
    assert {o["model"]: o["selectable"] for o in s["stt_model_options"]} == {"aer-stt-v1": True, "aer-stt-qwen3": False}
    assert client.patch("/api/settings", json={"stt_model": "aer-stt-qwen3"}).status_code == 409
    assert client.patch("/api/settings", json={"bogus": 1}).status_code == 400
    fake_catalog(monkeypatch, {"aer-stt-v1": "ready", "aer-stt-qwen3": "degraded"})
    r = client.patch("/api/settings", json={"stt_model": "aer-stt-qwen3", "video_retention_days": 0})
    assert r.status_code == 200 and r.json()["stt_model"] == "aer-stt-qwen3"
    assert db.effective_settings(settings).stt_model == "aer-stt-qwen3"
    assert client.get("/api/system").json()["stt"]["model"] == "aer-stt-qwen3"


def test_delete_meeting_purges_and_cancels(client, settings, monkeypatch):
    cancelled = []
    mod = types.ModuleType("quill.pipeline.stt")
    mod.cancel_jobs = lambda s, m: cancelled.append(m)
    monkeypatch.setitem(sys.modules, "quill.pipeline.stt", mod)
    monkeypatch.setattr(quill.pipeline, "stt", mod, raising=False)
    mid = seed(settings)
    assert client.delete(f"/api/meetings/{mid}").json() == {"ok": True}
    assert cancelled == [mid]
    assert not settings.media_dir(mid).exists()
    assert client.get(f"/api/meetings/{mid}").status_code == 404
    with db.opened(settings.db_path) as c:
        for t in ("meetings", "stages", "turns", "transcript_lines", "frames", "notes", "transcript_fts"):
            assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t


def test_delete_running_meeting_deferred_to_worker(client, settings):
    mid = seed(settings)
    with db.opened(settings.db_path) as c:
        c.execute("UPDATE stages SET status='running' WHERE meeting_id=? AND name='synthesize'", (mid,))
    client.delete(f"/api/meetings/{mid}")
    assert settings.media_dir(mid).exists()
    assert client.get(f"/api/meetings/{mid}").status_code == 404
    with db.opened(settings.db_path) as c:
        assert c.execute("SELECT status FROM meetings WHERE id=?", (mid,)).fetchone()[0] == "deleting"


def test_ownership(client, settings):
    mid = seed(settings)
    url = client.post("/api/invites", json={"email": "bob@example.com"}).json()["url"]
    with TestClient(client.app) as bob:
        bob.post("/api/auth/accept", json={"token": url.rsplit("/", 1)[1], "password": "bobs password"})
        assert bob.get("/api/meetings").json() == []
        for path in ("", "/transcript", "/notes", "/media", "/export?format=txt"):
            assert bob.get(f"/api/meetings/{mid}{path}").status_code == 404, path
        assert bob.get("/api/frames/1/image").status_code == 404
        assert bob.get("/api/search?q=budget").json()["transcript"] == []
        assert bob.delete(f"/api/meetings/{mid}").status_code == 404
        assert bob.patch("/api/settings", json={"video_retention_days": 1}).status_code == 403


def test_requires_login(settings):
    with TestClient(create_app(settings)) as c:
        for path in ("/api/meetings", "/api/search?q=x", "/api/system", "/api/settings", "/api/events"):
            r = c.get(path)
            assert r.status_code == 401 and "error" in r.json(), path
        assert c.get("/api/nope").status_code == 404


def test_spa_serving(settings, tmp_path):
    dist = settings.frontend_dist
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>quill</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    with TestClient(create_app(settings)) as c:
        assert c.get("/").text == "<html>quill</html>"
        assert c.get("/meetings/abc").text == "<html>quill</html>"
        assert c.get("/assets/app.js").text == "console.log(1)"
        assert "immutable" in c.get("/assets/app.js").headers["cache-control"]
        assert c.get("/api/unknown").json() == {"error": "Not found."}
