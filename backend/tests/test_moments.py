"""Tests for key-moment fusion (pure) and the moments stage (fake LLM + fake media)."""
from __future__ import annotations

import json
import types

import pytest

from quill import db as qdb
from quill.config import Settings
from quill.pipeline import llm, moments
from quill.pipeline.moments import (Candidate, fallback_candidates, frame_budget, fuse, hamming,
                                    normalize_scene_changes, scene_spans, select, split_windows)
from quill.stages import StageContext


# ---------------------------------------------------------------- pure fusion

def test_normalize_scene_changes_accepts_shapes():
    assert normalize_scene_changes([3.0, (1.0, 0.5), {"t": 2}, "x", -1, None]) == [1.0, 2.0, 3.0]


def test_scene_spans_clusters_close_changes():
    # 100, 101.5, 103 form one cluster (animation); stable span starts at 103
    spans = scene_spans([100.0, 101.5, 103.0, 200.0], duration=300.0)
    assert spans == [(0.0, 100.0), (103.0, 200.0), (200.0, 300.0)]


def test_text_moment_snaps_to_nearest_scene_change_within_20s():
    changes = [100.0, 400.0]
    text = [{"t": 112.0, "why": "slide on revenue", "look_for": "revenue chart"}]
    out = fuse(text, changes, duration=600.0)
    txt = [c for c in out if c.priority == 2]
    assert len(txt) == 1
    assert txt[0].source == "text+scene"
    assert txt[0].t == pytest.approx(101.5)  # cut + settle
    assert txt[0].why == "slide on revenue"


def test_text_moment_far_from_scene_change_keeps_its_time():
    out = fuse([{"t": 250.0, "why": "w", "look_for": "l"}], [100.0, 400.0], duration=600.0)
    txt = [c for c in out if c.priority == 2]
    assert txt[0].t == 250.0 and txt[0].source == "text"


def test_long_scenes_without_text_are_kept_short_ones_dropped():
    changes = [100.0, 120.0, 300.0]   # spans: 0-100, 100-120 (20 s), 120-300, 300-400
    out = fuse([], changes, duration=400.0)
    starts = sorted(c.scene_start for c in out)
    assert starts == [0.0, 120.0, 300.0]
    assert all(c.source == "scene" and c.priority == 1 for c in out)
    assert {c.weight for c in out} == {100.0, 180.0}


def test_text_backed_scene_suppresses_scene_candidate_and_merges_close_text():
    changes = [100.0, 300.0]
    text = [{"t": 105.0, "why": "a", "look_for": "x"}, {"t": 130.0, "why": "b", "look_for": "y"}]
    out = fuse(text, changes, duration=400.0)
    in_span = [c for c in out if 100.0 <= c.t < 300.0]
    assert len(in_span) == 1
    assert in_span[0].priority == 2 and in_span[0].why == "a; b"


def test_text_far_apart_in_one_long_span_stay_separate():
    out = fuse([{"t": 50.0, "why": "a", "look_for": ""}, {"t": 500.0, "why": "b", "look_for": ""}],
               [], duration=900.0)
    assert [c.t for c in out if c.priority == 2] == [50.0, 500.0]
    assert not [c for c in out if c.priority == 1]  # the only span is text-backed


def test_out_of_range_text_ignored():
    out = fuse([{"t": 9999.0, "why": "a"}, {"t": None}], [], duration=100.0)
    assert [c for c in out if c.priority == 2] == []


def test_budget():
    assert frame_budget(3600, 40, 200) == 40
    assert frame_budget(90 * 60, 40, 200) == 60
    assert frame_budget(10 * 3600, 40, 200) == 200
    assert frame_budget(10, 40, 200) == 1


def test_hamming_hex_and_int():
    assert hamming("ff", "0f") == 4
    assert hamming(0b1011, 0b0001) == 2


def test_select_prioritises_text_and_dedupes_by_phash():
    cands = [Candidate(t=10, source="scene", priority=1, weight=500),
             Candidate(t=20, source="text", why="a", priority=2),
             Candidate(t=30, source="text", why="b", priority=2),
             Candidate(t=40, source="scene", priority=1, weight=50),
             Candidate(t=50, source="sample", priority=0)]
    hashes = {10: "0000000000000000", 20: "ffffffffffffffff", 30: "fffffffffffffff0",  # 30 dup of 20
              40: "00000000ffffffff", 50: "0f0f0f0f0f0f0f0f"}
    kept = select(cands, budget=3, hash_fn=lambda c: hashes[int(c.t)])
    assert [c.t for c in kept] == [10, 20, 40]
    t20 = kept[1]
    assert t20.why == "a; b"          # the duplicate's reason is merged into the kept one
    assert t20.phash == "ffffffffffffffff"


def test_select_text_over_budget_spreads_over_time():
    cands = [Candidate(t=float(i * 60), source="text", priority=2) for i in range(20)]
    kept = select(cands, budget=4)
    ts = [c.t for c in kept]
    assert len(ts) == 4
    assert ts[0] < 300 and ts[-1] > 800   # not just the first four


def test_select_unknown_hash_is_not_deduped():
    cands = [Candidate(t=1, source="scene", priority=1, weight=2), Candidate(t=2, source="scene", priority=1, weight=1)]
    assert len(select(cands, budget=5, hash_fn=lambda c: None)) == 2


def test_fallback_scene_plus_fixed_sampling():
    out = fallback_candidates([600.0], duration=900.0)
    scenes = [c for c in out if c.source == "scene"]
    samples = [c for c in out if c.source == "sample"]
    assert [c.scene_start for c in scenes] == [0.0, 600.0]
    assert [c.t for c in samples] == [90.0, 270.0, 450.0, 810.0]  # 630 is covered by the scene at 601.5


def test_split_windows_at_line_boundaries():
    lines = [{"start": i * 60.0, "end": i * 60 + 50, "text": "x" * 40, "speaker": "S1"} for i in range(40)]
    ws = split_windows(lines, window_s=900)
    assert [len(w) for w in ws] == [15, 15, 10]
    ws = split_windows(lines, window_s=10_000, max_tokens=100)
    assert all(sum(len(ln["text"]) for ln in w) / 3.5 <= 100 for w in ws)


# ---------------------------------------------------------------- stage

def ok(content):
    body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}
    return 200, body, json.dumps(body)


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "q.sqlite3"
    qdb.init(dbp)
    conn = qdb.connect(dbp)
    with conn:
        mid = qdb.create_meeting(conn, owner_id=None, title="t", mode="video", source_path=str(tmp_path / "src.mp4"))
        conn.execute("UPDATE meetings SET duration_s=1800 WHERE id=?", (mid,))
    conn.close()
    (tmp_path / "src.mp4").write_bytes(b"fake")
    media_dir = tmp_path / "media" / mid
    media_dir.mkdir(parents=True)
    ctx = StageContext(meeting_id=mid, settings=Settings(data_dir=tmp_path, frames_per_hour=40, max_frames=200),
                       media_dir=media_dir, mode="video", db=lambda: qdb.connect(dbp))
    # never touch the network: catalog unknown, transport fake
    from quill.pipeline import catalog
    monkeypatch.setattr(catalog, "model_status", lambda *a, **k: "unknown")
    fake_media = types.SimpleNamespace(
        source_path=lambda ctx, row: tmp_path / "src.mp4",
        scene_changes=lambda src, thr, **kw: [300.0, 900.0],
        extract_frame=lambda src, t, out, edge, **kw: out.write_bytes(b"jpg") or (out, None),
        phash=lambda p: "0000000000000000",
    )
    monkeypatch.setattr(moments, "_media", lambda: fake_media)
    return ctx, dbp, fake_media


def add_lines(dbp, mid, lines):
    conn = qdb.connect(dbp)
    with conn:
        conn.executemany("INSERT INTO transcript_lines(meeting_id, speaker, start, end, text) VALUES (?,?,?,?,?)",
                         [(mid, s, a, b, t) for s, a, b, t in lines])
    conn.close()


def test_run_with_transcript(env, monkeypatch):
    ctx, dbp, fake_media = env
    add_lines(dbp, ctx.meeting_id, [("S1", 305.0, 312.0, "As you can see on this slide revenue is up"),
                                    ("S2", 1000.0, 1010.0, "ok")])
    requests = []

    def transport(url, payload, timeout):
        requests.append(payload)
        assert payload["response_format"]["type"] == "json_schema"
        user = payload["messages"][1]["content"]
        assert "[05:05] S1: As you can see" in user
        return ok(json.dumps({"moments": [{"t": "05:05", "why": "slide shown", "look_for": "revenue chart"},
                                          {"t": "99:00", "why": "hallucinated", "look_for": ""}]}))
    monkeypatch.setattr(llm, "default_transport", transport)
    hashes = iter(["0000000000000000", "ffffffffffffffff", "00000000ffffffff", "0f0f0f0f0f0f0f0f"])
    fake_media.phash = lambda p: next(hashes)
    moments.run(ctx)
    moments.run(ctx)   # idempotent: replaces rows
    conn = qdb.connect(dbp)
    rows = conn.execute("SELECT * FROM moments WHERE meeting_id=? ORDER BY t", (ctx.meeting_id,)).fetchall()
    conn.close()
    assert len(requests) == 2
    srcs = [(r["source"], r["t"]) for r in rows]
    assert ("text+scene", 301.5) in srcs
    assert all(r["why"] != "hallucinated" for r in rows)
    assert {r["source"] for r in rows} <= {"text+scene", "scene"}
    assert len(rows) == 3   # 0-300 scene, 300-900 text, 900-1800 scene
    assert not (ctx.media_dir / "frames" / "_probe").exists()


def test_run_without_transcript_uses_fallback(env, monkeypatch):
    ctx, dbp, _ = env

    def transport(*a):
        raise AssertionError("no LLM call without transcript")
    monkeypatch.setattr(llm, "default_transport", transport)
    moments.run(ctx)   # all phash equal -> everything dedupes to one frame
    conn = qdb.connect(dbp)
    rows = conn.execute("SELECT * FROM moments WHERE meeting_id=?", (ctx.meeting_id,)).fetchall()
    conn.close()
    assert len(rows) == 1 and rows[0]["source"] == "scene"


def test_run_audio_mode_is_noop(env):
    ctx, dbp, _ = env
    ctx.mode = "audio"
    moments.run(ctx)
