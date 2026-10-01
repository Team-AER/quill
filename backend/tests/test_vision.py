"""Tests for the frame-analysis stage (fake LLM + fake media, no network)."""
from __future__ import annotations

import json
import threading
import types

import pytest

from quill import db as qdb
from quill.config import Settings
from quill.pipeline import llm, vision
from quill.pipeline.vision import build_prompt, context_lines, normalize_result
from quill.stages import StageContext, StageFailed, StagePaused


def ok(content):
    body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}
    return 200, body, json.dumps(body)


RESULT = {"kind": "slide", "title": "Q3 revenue", "visible_text": "Revenue +12% QoQ",
          "key_facts": ["Revenue up 12%"], "relevance": 3, "caption": "Revenue slide while S1 explains growth"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "q.sqlite3"
    qdb.init(dbp)
    conn = qdb.connect(dbp)
    with conn:
        mid = qdb.create_meeting(conn, owner_id=None, title="t", mode="video", source_path=str(tmp_path / "src.mp4"))
        conn.executemany("INSERT INTO moments(meeting_id, t, source, why, look_for) VALUES (?,?,?,?,?)",
                         [(mid, 10.0, "text+scene", "slide", "revenue chart"),
                          (mid, 200.0, "scene", None, None),
                          (mid, 400.0, "scene", None, None)])
        conn.executemany("INSERT INTO transcript_lines(meeting_id, speaker, start, end, text) VALUES (?,?,?,?,?)",
                         [(mid, "S1", 5.0, 12.0, "revenue grew twelve percent"),
                          (mid, "S2", 100.0, 110.0, "far away line")])
    conn.close()
    media_dir = tmp_path / "media" / mid
    media_dir.mkdir(parents=True)
    ctx = StageContext(meeting_id=mid, settings=Settings(data_dir=tmp_path), media_dir=media_dir,
                       mode="video", db=lambda: qdb.connect(dbp))
    from quill.pipeline import catalog
    monkeypatch.setattr(catalog, "model_status", lambda *a, **k: "ready")
    extracted = []

    def extract_frame(src, t, out, edge, thumb=None, thumb_edge=320, **kw):
        extracted.append((t, edge, thumb_edge))
        out.write_bytes(b"\xff\xd8fakejpeg")
        if thumb is not None:
            thumb.write_bytes(b"\xff\xd8thumb")
        return out, thumb
    fake_media = types.SimpleNamespace(source_path=lambda ctx, row: tmp_path / "src.mp4",
                                       extract_frame=extract_frame)
    monkeypatch.setattr(vision, "_media", lambda: fake_media)
    return ctx, dbp, extracted


def frames(dbp, mid):
    conn = qdb.connect(dbp)
    try:
        return conn.execute("SELECT * FROM frames WHERE meeting_id=? ORDER BY t", (mid,)).fetchall()
    finally:
        conn.close()


def test_run_analyses_each_moment_and_indexes(env, monkeypatch):
    ctx, dbp, extracted = env
    seen = []
    lock = threading.Lock()

    def transport(url, payload, timeout):
        with lock:
            seen.append(payload)
        content = payload["messages"][1]["content"]
        assert content[0]["type"] == "image_url"
        assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert payload["response_format"]["json_schema"]["schema"] == vision.FRAME_SCHEMA
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        return ok(json.dumps(RESULT))
    monkeypatch.setattr(llm, "default_transport", transport)
    vision.run(ctx)
    rows = frames(dbp, ctx.meeting_id)
    assert len(rows) == 3 and len(seen) == 3
    assert rows[0]["kind"] == "slide" and rows[0]["relevance"] == 3
    assert json.loads(rows[0]["key_facts"]) == ["Revenue up 12%"]
    assert rows[0]["thumb_path"].endswith("_thumb.jpg")
    assert {e[1] for e in extracted} == {1280} and {e[2] for e in extracted} == {320}
    # context for t=10 contains only the nearby line, plus the look_for hint
    first = next(p for p in seen if "[00:10]" in p["messages"][1]["content"][1]["text"])
    text = first["messages"][1]["content"][1]["text"]
    assert "revenue grew twelve percent" in text and "far away line" not in text
    assert "revenue chart" in text
    conn = qdb.connect(dbp)
    hits = conn.execute("SELECT frame_id FROM frame_fts WHERE frame_fts MATCH 'revenue'").fetchall()
    conn.close()
    assert len(hits) == 3

    # resumable: second run makes no LLM calls
    seen.clear()
    vision.run(ctx)
    assert seen == [] and len(frames(dbp, ctx.meeting_id)) == 3


def test_failed_frame_is_kept_unanalysed_and_retried(env, monkeypatch):
    ctx, dbp, _ = env
    calls = {"n": 0}

    def transport(url, payload, timeout):
        calls["n"] += 1
        if "[00:10]" in payload["messages"][1]["content"][1]["text"]:
            return 400, None, "bad image"
        return ok(json.dumps(RESULT))
    monkeypatch.setattr(llm, "default_transport", transport)
    vision.run(ctx)
    rows = frames(dbp, ctx.meeting_id)
    assert len(rows) == 3
    assert rows[0]["kind"] is None and rows[0]["path"]
    assert rows[1]["kind"] == "slide"
    calls["n"] = 0
    monkeypatch.setattr(llm, "default_transport", lambda *a: ok(json.dumps(RESULT)))
    vision.run(ctx)
    rows = frames(dbp, ctx.meeting_id)
    assert len(rows) == 3 and all(r["kind"] == "slide" for r in rows)


def test_mostly_failing_raises_stage_failed(env, monkeypatch):
    ctx, _, _ = env
    monkeypatch.setattr(llm, "default_transport", lambda *a: (400, None, "nope"))
    with pytest.raises(StageFailed):
        vision.run(ctx)


def test_disabled_model_pauses(env, monkeypatch):
    ctx, _, _ = env
    from quill.pipeline import catalog
    monkeypatch.setattr(catalog, "model_status", lambda *a, **k: "disabled")
    with pytest.raises(StagePaused):
        vision.run(ctx)


def test_stale_frames_removed_when_moments_change(env, monkeypatch):
    ctx, dbp, _ = env
    monkeypatch.setattr(llm, "default_transport", lambda *a: ok(json.dumps(RESULT)))
    vision.run(ctx)
    conn = qdb.connect(dbp)
    with conn:
        conn.execute("DELETE FROM moments WHERE meeting_id=? AND t=400", (ctx.meeting_id,))
    conn.close()
    vision.run(ctx)
    assert [r["t"] for r in frames(dbp, ctx.meeting_id)] == [10.0, 200.0]


def test_helpers():
    lines = [{"speaker": "S1", "start": float(i * 10), "end": float(i * 10 + 9), "text": "w " * 50}
             for i in range(30)]
    sel = context_lines(lines, 150.0, span=60, max_tokens=10_000)
    assert sel[0]["start"] == 90.0 and sel[-1]["start"] == 210.0
    small = context_lines(lines, 150.0, span=60, max_tokens=100)
    assert all(abs(ln["start"] - 150) <= 30 for ln in small)
    assert "Hint" in build_prompt(5.0, "chart", None, [])
    n = normalize_result({"kind": "weird", "relevance": 9, "key_facts": ["", "a"], "title": None})
    assert n["kind"] == "other" and n["relevance"] == 3 and n["key_facts"] == ["a"] and n["title"] == ""
