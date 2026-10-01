"""Tests for map-reduce synthesis (fake LLM, no network)."""
from __future__ import annotations

import json
import re

import pytest

from quill import db as qdb
from quill.config import Settings
from quill.pipeline import llm, synthesis
from quill.pipeline.synthesis import (build_reduce_input, ground_items, is_grounded, merge_reduced,
                                      salient_words, speaker_suggestions, synthesize)
from quill.stages import StageContext, StagePaused


def ok(obj):
    content = obj if isinstance(obj, str) else json.dumps(obj)
    body = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}
    return 200, body, json.dumps(body)


def L(speaker, start, text, end=None):
    return {"id": int(start), "speaker": speaker, "start": float(start), "end": float(end or start + 8), "text": text}


# ---------------------------------------------------------------- grounding

LINES = [L("S1", 10, "Hi everyone, I'm Priya from finance."),
         L("S2", 30, "Thanks Priya. Let's ship the beta on Friday."),
         L("S1", 50, "Agreed, beta ships Friday. Tom will update the pricing page."),
         L("S2", 400, "Unrelated chatter about lunch options.")]


def test_salient_words_drops_stopwords_and_stems():
    w = salient_words("We agreed to ship the Beta release on Fridays, 2026")
    assert {"ship", "beta", "release", "friday", "2026"} <= w
    assert "agreed" not in w and "the" not in w


def test_is_grounded():
    assert is_grounded("Ship the beta on Friday", 35.0, LINES, 500)
    assert is_grounded("Tom updates the pricing page", 55.0, LINES, 500)
    assert not is_grounded("Ship the beta on Friday", 400.0 + 200, LINES, 700)   # nothing nearby
    assert not is_grounded("Hire three engineers in Berlin", 35.0, LINES, 500)
    assert not is_grounded("Ship the beta on Friday", 9999.0, LINES, 500)       # outside meeting
    assert not is_grounded("Ship the beta", None, LINES, 500)


def test_reduce_merge_takes_earliest_source_t_and_drops_unknown_ids():
    secs = [{"start": 0, "end": 60, "title": "Launch", "summary": "s",
             "decisions": [{"text": "Ship beta Friday", "t": 35.0}],
             "action_items": [{"owner": "Tom", "task": "Update pricing page", "due": None, "t": 55.0}],
             "open_questions": []},
            {"start": 60, "end": 500, "title": "Other", "summary": "s2",
             "decisions": [{"text": "Beta ships Friday", "t": 50.0}], "action_items": [], "open_questions": []}]
    text, catalog = build_reduce_input(secs)
    assert "[D1] decision: Ship beta Friday" in text and "[A1] action: Update pricing page (owner: Tom)" in text
    reduced = {"decisions": [{"text": "Ship the beta on Friday", "source_ids": ["D2", "[d1]"]},
                             {"text": "Invented", "source_ids": ["D99"]}],
               "action_items": [{"owner": "", "task": "Update the pricing page", "due": "none", "source_ids": ["A1"]}],
               "open_questions": [{"text": "wrong kind", "source_ids": ["D1"]}]}
    merged = merge_reduced(reduced, catalog)
    assert [d["t"] for d in merged["decisions"]] == [35.0]
    assert merged["action_items"][0]["owner"] == "Tom" and merged["action_items"][0]["due"] is None
    assert merged["open_questions"] == []
    g = ground_items(merged, LINES, 500)
    assert g["decisions"][0] == {"text": "Ship the beta on Friday", "t": 35.0, "grounded": True}
    assert g["action_items"][0]["grounded"] is True


def test_reduce_input_truncates_summaries_to_budget():
    secs = [{"start": i * 1200, "end": (i + 1) * 1200, "title": "T", "summary": "x" * 20000,
             "decisions": [], "action_items": [], "open_questions": []} for i in range(24)]
    text, _ = build_reduce_input(secs, max_tokens=10000)
    assert llm.estimate_tokens(text) < 12000


def test_speaker_suggestions_require_name_in_transcript():
    secs = [{"speaker_names": [{"label": "S1", "name": "Priya", "evidence_t": 10.0},
                               {"label": "S2", "name": "Bob", "evidence_t": 30.0},       # not in transcript
                               {"label": "S9", "name": "Priya", "evidence_t": 10.0}]},   # unknown label
            {"speaker_names": [{"label": "S1", "name": "Priya", "evidence_t": 30.0}]}]
    out = speaker_suggestions(secs, LINES, {"S1", "S2"})
    assert out == [{"label": "S1", "name": "Priya", "evidence_t": 10.0}]


# ---------------------------------------------------------------- end to end with fake LLM

def make_fake(map_hook=None, reduce_obj=None, fail_reduce=False):
    calls = {"map": 0, "reduce": 0, "max_prompt_tokens": 0}

    def transport(url, payload, timeout):
        name = payload["response_format"]["json_schema"]["name"]
        prompt = "".join(m["content"] for m in payload["messages"] if isinstance(m["content"], str))
        calls["max_prompt_tokens"] = max(calls["max_prompt_tokens"], llm.estimate_tokens(prompt))
        if name == "section_notes":
            calls["map"] += 1
            user = payload["messages"][1]["content"]
            first_ts = re.search(r"\n\[(\d[\d:]*)\] S", user).group(1)
            obj = {"title": f"Part at {first_ts}", "summary": "Discussed things.", "decisions": [],
                   "action_items": [], "open_questions": [], "speaker_names": []}
            if map_hook:
                map_hook(user, obj)
            return ok(obj)
        calls["reduce"] += 1
        if fail_reduce:
            return 500, None, "boom"
        return ok(reduce_obj or {"tldr": ["Beta ships Friday"], "summary": "Launch planning.",
                                 "decisions": [], "action_items": [], "open_questions": []})
    return transport, calls


def test_synthesize_small_meeting_all_sections():
    def hook(user, obj):
        if "[00:30] S2" in user:
            obj["decisions"] = [{"text": "Ship the beta on Friday", "t": "00:30"}]
            obj["action_items"] = [{"owner": "Tom", "task": "Update the pricing page", "due": "", "t": "00:50"},
                                   {"owner": "S2", "task": "Book a venue in Lisbon", "due": "", "t": "00:30"}]
            obj["open_questions"] = [{"text": "Do we need legal review?", "t": "garbage"}]
            obj["speaker_names"] = [{"label": "S1", "name": "Priya", "evidence_t": "00:10"}]
    reduce_obj = {"tldr": ["Beta ships Friday"], "summary": "Launch planning.",
                  "decisions": [{"text": "Ship the beta on Friday", "source_ids": ["D1"]}],
                  "action_items": [{"owner": "Tom", "task": "Update the pricing page", "due": "", "source_ids": ["A1"]},
                                   {"owner": "S2", "task": "Book a venue in Lisbon", "due": "", "source_ids": ["A2"]}],
                  "open_questions": [{"text": "Do we need legal review?", "source_ids": ["Q1"]}]}
    transport, calls = make_fake(hook, reduce_obj)
    c = llm.LLMClient("m", "http://gw.test/v1", transport=transport, sleep=lambda s: None)
    frames = [{"id": 7, "t": 20.0, "kind": "slide", "title": "Roadmap", "caption": "Roadmap slide",
               "visible_text": "Beta: Friday", "key_facts": '["Beta Friday"]', "relevance": 3},
              {"id": 8, "t": 40.0, "kind": "people", "title": "", "caption": "faces",
               "visible_text": "", "key_facts": "[]", "relevance": 1}]
    notes = synthesize(c, LINES, frames, [{"label": "S1", "display_name": None}, {"label": "S2", "display_name": None}], 500)
    assert calls == {"map": 1, "reduce": 1, "max_prompt_tokens": calls["max_prompt_tokens"]}
    assert set(notes) == {"tldr", "summary", "chapters", "decisions", "action_items", "open_questions",
                          "key_visuals", "speaker_suggestions"}
    assert notes["tldr"] == ["Beta ships Friday"]
    assert notes["chapters"][0]["start"] == 10.0 and notes["chapters"][0]["title"].startswith("Part at")
    assert notes["decisions"] == [{"text": "Ship the beta on Friday", "t": 30.0, "grounded": True}]
    by_task = {a["task"]: a for a in notes["action_items"]}
    assert by_task["Update the pricing page"]["grounded"] is True
    assert by_task["Book a venue in Lisbon"]["grounded"] is False     # not in the transcript
    assert by_task["Book a venue in Lisbon"]["owner"] == "S2" and by_task["Update the pricing page"]["due"] is None
    assert notes["open_questions"][0]["grounded"] is False            # unparseable t
    assert notes["key_visuals"] == [{"frame_id": 7, "t": 20.0, "caption": "Roadmap slide"}]
    assert notes["speaker_suggestions"] == [{"label": "S1", "name": "Priya", "evidence_t": 10.0}]


def test_eight_hour_meeting_stays_within_budget(tmp_path):
    lines = [L(f"S{1 + i % 3}", i * 6.0, "We talked about the quarterly roadmap and the budget numbers " * 2)
             for i in range(int(8 * 3600 / 6))]
    transport, calls = make_fake()
    c = llm.LLMClient("m", "http://gw.test/v1", transport=transport, sleep=lambda s: None)
    notes = synthesize(c, lines, [], [], 8 * 3600, concurrency=4, cache_dir=tmp_path / "cache")
    assert calls["map"] == 24 and calls["reduce"] == 1
    assert calls["max_prompt_tokens"] < 60000
    assert len(notes["chapters"]) == 24
    assert notes["chapters"][-1]["end"] >= 8 * 3600 - 10
    # cached map results: a re-run only repeats the reduce call
    transport2, calls2 = make_fake()
    c2 = llm.LLMClient("m", "http://gw.test/v1", transport=transport2, sleep=lambda s: None)
    synthesize(c2, lines, [], [], 8 * 3600, cache_dir=tmp_path / "cache")
    assert calls2["map"] == 0 and calls2["reduce"] == 1


def test_reduce_failure_falls_back_to_section_items():
    def hook(user, obj):
        obj["decisions"] = [{"text": "Ship the beta on Friday", "t": "00:30"}]
    transport, _ = make_fake(hook, fail_reduce=True)
    c = llm.LLMClient("m", "http://gw.test/v1", transport=transport, sleep=lambda s: None, retries=0)
    notes = synthesize(c, LINES, [], [], 500)
    assert notes["decisions"][0]["t"] == 30.0 and notes["decisions"][0]["grounded"]
    assert notes["summary"] == "Discussed things." and notes["tldr"]


def test_empty_transcript_no_llm():
    c = llm.LLMClient("m", "http://gw.test/v1", transport=lambda *a: pytest.fail("no call"))
    notes = synthesize(c, [], [], [], 100)
    assert notes["chapters"] == [] and notes["summary"]


# ---------------------------------------------------------------- stage

@pytest.fixture
def env(tmp_path, monkeypatch):
    dbp = tmp_path / "q.sqlite3"
    qdb.init(dbp)
    conn = qdb.connect(dbp)
    with conn:
        mid = qdb.create_meeting(conn, owner_id=None, title="t", mode="audio")
        conn.execute("UPDATE meetings SET duration_s=500 WHERE id=?", (mid,))
        conn.executemany("INSERT INTO speakers(meeting_id, label) VALUES (?,?)", [(mid, "S1"), (mid, "S2")])
        conn.executemany("INSERT INTO transcript_lines(meeting_id, speaker, start, end, text) VALUES (?,?,?,?,?)",
                         [(mid, ln["speaker"], ln["start"], ln["end"], ln["text"]) for ln in LINES])
        conn.execute("INSERT INTO frames(meeting_id, moment_id, t, kind, relevance, caption) VALUES (?,?,?,?,?,?)",
                     (mid, 1, 20.0, "slide", 3, "should not appear in audio mode"))
    conn.close()
    media_dir = tmp_path / "media" / mid
    media_dir.mkdir(parents=True)
    ctx = StageContext(meeting_id=mid, settings=Settings(data_dir=tmp_path), media_dir=media_dir,
                       mode="audio", db=lambda: qdb.connect(dbp))
    from quill.pipeline import catalog
    monkeypatch.setattr(catalog, "model_status", lambda *a, **k: "ready")
    return ctx, dbp


def test_run_writes_notes_versions_and_speaker_suggestions(env, monkeypatch):
    ctx, dbp = env

    def hook(user, obj):
        assert "Frames" not in user     # audio mode: no visuals
        obj["speaker_names"] = [{"label": "S1", "name": "Priya", "evidence_t": "00:10"}]
    transport, _ = make_fake(hook)
    monkeypatch.setattr(llm, "default_transport", transport)
    synthesis.run(ctx)
    synthesis.run(ctx)
    conn = qdb.connect(dbp)
    row = conn.execute("SELECT * FROM notes WHERE meeting_id=?", (ctx.meeting_id,)).fetchone()
    spk = {r["label"]: r for r in conn.execute("SELECT * FROM speakers WHERE meeting_id=?", (ctx.meeting_id,))}
    conn.close()
    assert row["version"] == 2 and row["model"] == Settings().text_model
    notes = json.loads(row["json"])
    assert notes["key_visuals"] == []
    assert spk["S1"]["suggested_name"] == "Priya" and spk["S1"]["suggestion_evidence_t"] == 10.0
    assert spk["S2"]["suggested_name"] is None


def test_run_pauses_when_text_model_disabled(env, monkeypatch):
    ctx, _ = env
    from quill.pipeline import catalog
    monkeypatch.setattr(catalog, "model_status", lambda *a, **k: "disabled")
    monkeypatch.setattr(llm, "default_transport", lambda *a: pytest.fail("no call"))
    with pytest.raises(StagePaused):
        synthesis.run(ctx)
