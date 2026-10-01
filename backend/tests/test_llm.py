"""Tests for quill.pipeline.llm with a fake transport (no network)."""
from __future__ import annotations

import base64
import json

import pytest

from quill.pipeline import llm
from quill.pipeline.llm import (LLMClient, LLMError, LLMOutputError, estimate_tokens, fmt_ts,
                                parse_json_loose, parse_ts, render_lines, user_message, validate)
from quill.stages import StagePaused

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"},
                   "score": {"type": "integer", "enum": [0, 1, 2, 3]}},
    "required": ["name", "score"],
    "additionalProperties": False,
}


def ok(content: str) -> tuple[int, dict, str]:
    body = {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return 200, body, json.dumps(body)


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, float]] = []

    def __call__(self, url, payload, timeout):
        self.calls.append((url, json.loads(json.dumps(payload)), timeout))
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


def client(transport, **kw):
    kw.setdefault("sleep", lambda s: None)
    return LLMClient("m", "http://gw.test/v1/", transport=transport, **kw)


def test_request_shape_json_schema_and_thinking_off():
    t = FakeTransport([ok('{"name": "x", "score": 2}')])
    out = client(t).chat_json([{"role": "user", "content": "hi"}], SCHEMA, name="thing")
    assert out == {"name": "x", "score": 2}
    url, payload, timeout = t.calls[0]
    assert url == "http://gw.test/v1/chat/completions"
    assert payload["model"] == "m"
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    rf = payload["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "thing"
    assert rf["json_schema"]["schema"] == SCHEMA
    assert timeout == 60.0


def test_image_data_url(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"rest"
    p = tmp_path / "a.png"
    p.write_bytes(png)
    msg = user_message("describe", [p, b"\xff\xd8jpeg"])
    parts = msg["content"]
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(png).decode()
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[2] == {"type": "text", "text": "describe"}
    assert user_message("plain") == {"role": "user", "content": "plain"}


def test_retries_5xx_and_timeouts_then_succeeds():
    sleeps = []
    t = FakeTransport([(502, None, "bad gateway"), TimeoutError("slow"), ok('{"name":"a","score":0}')])
    c = client(t, sleep=sleeps.append, backoff=1.5)
    assert c.chat_json([{"role": "user", "content": "x"}], SCHEMA)["name"] == "a"
    assert len(t.calls) == 3
    assert sleeps == [1.5, 3.0]


def test_gives_up_after_two_retries():
    t = FakeTransport([(500, None, "")] * 3)
    with pytest.raises(LLMError, match="3 attempts"):
        client(t).chat([{"role": "user", "content": "x"}])
    assert len(t.calls) == 3


def test_4xx_is_not_retried():
    t = FakeTransport([(422, {"error": "nope"}, '{"error":"nope"}')])
    with pytest.raises(LLMError, match="HTTP 422"):
        client(t).chat([{"role": "user", "content": "x"}])
    assert len(t.calls) == 1


def test_fallback_to_json_object_when_schema_rejected():
    t = FakeTransport([(400, None, "response_format json_schema is not supported"),
                       ok('```json\n{"name": "b", "score": 1}\n```'),
                       ok('{"name": "c", "score": 3}')])
    c = client(t)
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x"}]
    assert c.chat_json(msgs, SCHEMA)["name"] == "b"
    assert c.schema_mode == "json_object"
    p2 = t.calls[1][1]
    assert p2["response_format"] == {"type": "json_object"}
    assert "JSON schema" in p2["messages"][0]["content"] and p2["messages"][0]["content"].startswith("sys")
    # remembered for later calls
    assert c.chat_json(msgs, SCHEMA)["name"] == "c"
    assert t.calls[2][1]["response_format"] == {"type": "json_object"}


def test_repair_retry_then_success():
    t = FakeTransport([ok('{"name": "a", "score": 7}'), ok('{"name": "a", "score": 3}')])
    assert client(t).chat_json([{"role": "user", "content": "x"}], SCHEMA)["score"] == 3
    repair_msgs = t.calls[1][1]["messages"]
    assert repair_msgs[-2]["role"] == "assistant"
    assert "not in enum" in repair_msgs[-1]["content"]


def test_repair_retry_then_failure():
    t = FakeTransport([ok("not json at all"), ok('{"name": 1}')])
    with pytest.raises(LLMOutputError):
        client(t).chat_json([{"role": "user", "content": "x"}], SCHEMA)


def test_catalog_disabled_pauses_unknown_proceeds():
    t = FakeTransport([ok('{"name":"a","score":0}')])
    with pytest.raises(StagePaused, match="disabled"):
        client(t, status_lookup=lambda m: "disabled").chat([{"role": "user", "content": "x"}])
    assert t.calls == []
    c = client(t, status_lookup=lambda m: "unknown")
    c.chat([{"role": "user", "content": "x"}])
    assert len(t.calls) == 1


def test_status_cached_between_calls():
    seen = []
    t = FakeTransport([ok("{}"), ok("{}")])
    c = client(t, status_lookup=lambda m: seen.append(m) or "ready")
    c.chat([{"role": "user", "content": "x"}])
    c.chat([{"role": "user", "content": "x"}])
    assert seen == ["m"]


def test_default_status_lookup_uses_catalog(monkeypatch):
    from quill.pipeline import catalog
    calls = []

    def fake_status(settings, model_id, **kw):
        calls.append((settings, model_id))
        return "disabled"
    monkeypatch.setattr(catalog, "model_status", fake_status)
    lookup = llm.default_status_lookup("S")
    assert lookup("gemma") == "disabled"
    assert calls == [("S", "gemma")]

    def boom(*a, **k):
        raise RuntimeError("catalog down")
    monkeypatch.setattr(catalog, "model_status", boom)
    assert lookup("gemma") is None  # unknown -> proceed


def test_gateway_disabled_response_pauses():
    t = FakeTransport([(503, None, '{"error": "model aer-x is disabled by admin"}')])
    with pytest.raises(StagePaused):
        client(t).chat([{"role": "user", "content": "x"}])


def test_from_settings_reads_lowercase_fields():
    from quill.config import Settings
    s = Settings(gateway_url="http://gw.example/v1", vision_model="vis", text_model="txt")
    c = LLMClient.from_settings(s, "VISION_MODEL", transport=lambda *a: ok("{}"))
    assert c.model == "vis" and c.base_url == "http://gw.example/v1"


def test_helpers():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 35) == 10
    assert estimate_tokens("a" * 36) == 11
    assert fmt_ts(65) == "01:05" and fmt_ts(3725) == "1:02:05"
    assert parse_ts("01:05") == 65 and parse_ts("[1:02:05]") == 3725 and parse_ts(12.5) == 12.5
    assert parse_ts("75:00") == 4500 and parse_ts("garbage") is None and parse_ts(None) is None
    assert parse_json_loose('here: {"a": 1} done') == {"a": 1}
    assert validate({"name": "x", "score": 1, "extra": 1}, SCHEMA) == ["$.extra: unexpected property"]
    lines = [{"speaker": "S2", "start": 61.0, "text": " hello "}, {"speaker": "S1", "start": 70, "text": ""}]
    assert render_lines(lines) == "[01:01] S2: hello"
