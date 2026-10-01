"""llm-proxy model status (ready / degraded / disabled / quarantined / missing / unknown).

Source of truth: ``<gateway>/catalog.json`` -- the sanitized catalog the
llm-proxy gateway regenerates every 30 s. It is the same URL Avifors uses as ``server.audio.admission_catalog_url``
to admit/pause STT work, so Quill pauses exactly when Avifors does.

Catalog shape (verified live 2026-09-30)::

    {"generated_at": "...", "models": [
        {"id": "aer-stt-v1", "deployment_id": "avifors-omnilingual-3b",
         "display_name": "...", "capabilities": ["transcription", ...],
         "status": "ready" | "degraded" | "offline" | "disabled",
         "disabled_at": "..."   # only on disabled / quarantined deployments
        }, ...]}

One public model id can have several deployments (e.g. an active one plus
disabled rollback deployments). Avifors admits a model if *any* entry is
ready/degraded, so we aggregate the same way. Catalog ``offline`` means the
health probe quarantined the deployment (3 failed probes -> quarantine), which we
report as ``quarantined``.

Read-only: this module only ever GETs the catalog.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

CACHE_S = 10.0
TIMEOUT_S = 5.0
USABLE = frozenset({"ready", "degraded"})
# Higher wins when a model id has several deployments.
_RANK = {"ready": 5, "degraded": 4, "disabled": 3, "quarantined": 2, "missing": 1, "unknown": 0}

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
# Tests may swap the transport (httpx.MockTransport).
transport: httpx.BaseTransport | None = None


def setting(settings: Any, name: str, default: Any = None) -> Any:
    """Read a Settings field tolerant of UPPER / lower attribute naming."""
    for key in (name, name.lower(), name.upper()):
        if hasattr(settings, key):
            value = getattr(settings, key)
            if value is not None:
                return value
    return default


def catalog_url(settings: Any) -> str:
    explicit = setting(settings, "CATALOG_URL")
    if explicit:
        return str(explicit)
    base = str(setting(settings, "GATEWAY_URL", "http://localhost:4000/v1")).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/catalog.json"


def _normalise(entry: dict[str, Any]) -> str:
    raw = str(entry.get("status") or "").lower()
    if raw in ("ready", "degraded", "disabled"):
        return raw
    if raw in ("offline", "quarantined", "quarantine"):
        return "quarantined"
    return "unknown"


def fetch_catalog(settings: Any, *, force: bool = False) -> dict[str, Any] | None:
    """Return the parsed catalog (cached 10 s) or None when unreachable/invalid."""
    url = catalog_url(settings)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(url)
        if hit and not force and now - hit[0] < CACHE_S:
            return hit[1]
    payload: dict[str, Any] | None
    try:
        with httpx.Client(timeout=TIMEOUT_S, transport=transport) as client:
            response = client.get(url, headers={"Cache-Control": "no-cache"})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            payload = None
    except (httpx.HTTPError, ValueError):
        payload = None
    with _lock:
        _cache[url] = (time.monotonic(), payload)
    return payload


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def model_statuses(settings: Any, *, force: bool = False) -> dict[str, str]:
    """{model_id: status} for every model in the catalog. Empty dict when the
    catalog is unreachable (callers then get "unknown" from model_status)."""
    payload = fetch_catalog(settings, force=force)
    if payload is None:
        return {}
    out: dict[str, str] = {}
    for entry in payload["models"]:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        mid = str(entry["id"])
        status = _normalise(entry)
        if mid not in out or _RANK[status] > _RANK[out[mid]]:
            out[mid] = status
    return out


def model_status(settings: Any, model_id: str, *, force: bool = False) -> str:
    payload = fetch_catalog(settings, force=force)
    if payload is None:
        return "unknown"
    return model_statuses(settings).get(model_id, "missing")


def is_usable(status: str) -> bool:
    return status in USABLE


def models_with_capability(settings: Any, capability: str) -> list[dict[str, Any]]:
    """[{id, display_name, status}] for models advertising a capability, e.g.
    "transcription" for the admin STT model picker (only ready/degraded may be
    selected)."""
    payload = fetch_catalog(settings)
    if payload is None:
        return []
    statuses = model_statuses(settings)
    seen: dict[str, dict[str, Any]] = {}
    for entry in payload["models"]:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if capability not in (entry.get("capabilities") or []):
            continue
        mid = str(entry["id"])
        if mid in seen and statuses.get(mid) != _normalise(entry):
            continue  # keep the display name of the deployment that set the status
        seen[mid] = {
            "id": mid,
            "display_name": entry.get("display_name") or mid,
            "status": statuses.get(mid, "unknown"),
        }
    return list(seen.values())
