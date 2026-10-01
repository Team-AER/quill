"""llm-proxy catalog status (read-only GET of /catalog.json)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest

from quill.pipeline import catalog


@dataclass(frozen=True)
class S:
    gateway_url: str = "http://gw.test/v1"


LIVE_SHAPE = {
    "generated_at": "2026-09-29T21:46:52+00:00",
    "models": [
        {"id": "aer-stt-v1", "deployment_id": "avifors-omnilingual-3b", "status": "ready",
         "display_name": "Multilingual STT", "capabilities": ["transcription", "async_jobs"]},
        {"id": "aer-stt-qwen3", "deployment_id": "avifors-qwen3-asr", "status": "degraded",
         "display_name": "Qwen3-ASR", "capabilities": ["transcription"]},
        {"id": "aer-image-v1", "deployment_id": "avifors-dreamshaper8", "status": "ready",
         "capabilities": ["image_generation"]},
        {"id": "aer-image-v1", "deployment_id": "gpu1-dreamshaper8", "status": "disabled",
         "disabled_at": "2026-09-26", "capabilities": ["image_generation"]},
        {"id": "qwen3.8:27b", "deployment_id": "ollama", "status": "offline", "disabled_at": "x"},
        {"id": "gemma", "deployment_id": "g", "status": "disabled", "disabled_at": "x",
         "capabilities": ["chat"]},
        {"id": "weird", "deployment_id": "w", "status": "booting"},
    ],
}


@pytest.fixture(autouse=True)
def mock(monkeypatch):
    calls = []
    state = {"payload": LIVE_SHAPE, "status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        if state["status"] != 200:
            return httpx.Response(state["status"])
        return httpx.Response(200, json=state["payload"])

    monkeypatch.setattr(catalog, "transport", httpx.MockTransport(handler))
    catalog.clear_cache()
    yield calls, state
    catalog.clear_cache()


def test_catalog_url_derivation():
    assert catalog.catalog_url(S()) == "http://gw.test/catalog.json"
    assert catalog.catalog_url(S("http://gateway.lan/v1/")) == "http://gateway.lan/catalog.json"

    @dataclass(frozen=True)
    class Explicit:
        gateway_url: str = "http://a/v1"
        catalog_url: str = "http://b/c.json"

    assert catalog.catalog_url(Explicit()) == "http://b/c.json"


def test_statuses(mock):
    calls, _ = mock
    st = catalog.model_statuses(S())
    assert st["aer-stt-v1"] == "ready"
    assert st["aer-stt-qwen3"] == "degraded"
    assert st["aer-image-v1"] == "ready"  # any usable deployment wins, as in Avifors
    assert st["qwen3.8:27b"] == "quarantined"  # catalog "offline"
    assert st["gemma"] == "disabled"
    assert st["weird"] == "unknown"
    assert calls[0].url == "http://gw.test/catalog.json"


def test_model_status_and_missing(mock):
    assert catalog.model_status(S(), "aer-stt-v1") == "ready"
    assert catalog.model_status(S(), "nope") == "missing"
    assert catalog.is_usable("ready") and catalog.is_usable("degraded")
    assert not catalog.is_usable("disabled") and not catalog.is_usable("unknown")


def test_cache_10s(mock, monkeypatch):
    calls, state = mock
    now = [1000.0]
    monkeypatch.setattr(catalog.time, "monotonic", lambda: now[0])
    catalog.model_status(S(), "aer-stt-v1")
    catalog.model_status(S(), "aer-stt-v1")
    assert len(calls) == 1
    state["payload"] = {"models": [{"id": "aer-stt-v1", "status": "disabled"}]}
    now[0] += 5
    assert catalog.model_status(S(), "aer-stt-v1") == "ready"
    now[0] += 6
    assert catalog.model_status(S(), "aer-stt-v1") == "disabled"
    assert len(calls) == 2
    assert catalog.model_status(S(), "aer-stt-v1", force=True) == "disabled"
    assert len(calls) == 3


def test_unreachable_is_unknown(mock):
    _, state = mock
    state["status"] = 502
    assert catalog.model_status(S(), "aer-stt-v1") == "unknown"
    assert catalog.model_statuses(S()) == {}


def test_invalid_payload_is_unknown(mock):
    _, state = mock
    state["payload"] = {"data": []}
    assert catalog.model_status(S(), "aer-stt-v1") == "unknown"


def test_transport_error_is_unknown(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(catalog, "transport", httpx.MockTransport(boom))
    catalog.clear_cache()
    assert catalog.model_status(S(), "aer-stt-v1") == "unknown"


def test_models_with_capability(mock):
    stt = catalog.models_with_capability(S(), "transcription")
    assert {m["id"]: m["status"] for m in stt} == {"aer-stt-v1": "ready", "aer-stt-qwen3": "degraded"}
    assert stt[0]["display_name"] == "Multilingual STT"


def test_setting_helper_upper_and_lower():
    class Upper:
        GATEWAY_URL = "http://u/v1"

    assert catalog.setting(Upper(), "GATEWAY_URL") == "http://u/v1"
    assert catalog.setting(S(), "GATEWAY_URL") == "http://gw.test/v1"
    assert catalog.setting(S(), "MISSING", 3) == 3
