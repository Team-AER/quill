"""OpenAI-compatible chat client for llm-proxy (owner: C2).

Every request goes through ``settings.GATEWAY_URL`` (QUILL_GATEWAY_URL, an
OpenAI-compatible gateway, keyless on the LAN). Never point this at a backend model server directly.

Features
- JSON-schema structured output via ``response_format: {type: json_schema}``.
  Verified 2026-09-30 on the gateway (LiteLLM -> vLLM 0.26 guided decoding):
  the schema is enforced, including enums. If the gateway ever rejects
  ``json_schema`` (HTTP 400 mentioning response_format), the client falls back
  to ``json_object`` + local validation + one repair retry, and remembers that
  for the rest of the process.
- Images as base64 ``image_url`` data URLs.
- ``chat_template_kwargs: {"enable_thinking": false}`` by default.
- Timeouts; 2 retries with exponential backoff on 5xx / 429 / timeouts / conn errors.
- ``StagePaused`` when the model's catalog status is disabled/quarantined
  (via ``quill.pipeline.catalog.model_status``, imported lazily; unknown -> proceed),
  or when the gateway rejects a request because the model is disabled.

Never logs prompt or completion text (meetings are sensitive).
"""
from __future__ import annotations

import base64
import json
import logging
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

log = logging.getLogger("quill.llm")

DEFAULT_GATEWAY = "http://localhost:4000/v1"
DEFAULT_MODEL = "google/gemma-4-12B-it-qat-w4a16-ct"
PROMPTS_DIR = Path(__file__).with_name("prompts")

# (status, parsed_json_body_or_None, raw_text)
Transport = Callable[[str, dict, float], "tuple[int, Any, str]"]

# Process-wide transport override (tests install a fake here; None = HTTP).
default_transport: Transport | None = None


class LLMError(Exception):
    """The LLM call failed after retries, or returned unusable output."""


class LLMOutputError(LLMError):
    """The model answered but the JSON did not validate (after the repair retry)."""


# ---------------------------------------------------------------- helpers

def estimate_tokens(text: str) -> int:
    """Cheap token estimate: chars / 3.5 (good enough for budget planning)."""
    if not text:
        return 0
    return int(math.ceil(len(text) / 3.5))


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def setting(settings: Any, name: str, default: Any = None) -> Any:
    """Read a Settings attribute tolerant of UPPER/lower naming."""
    if settings is None:
        return default
    for key in (name, name.lower(), name.upper()):
        if hasattr(settings, key):
            val = getattr(settings, key)
            if val is not None:
                return val
    return default


def image_data_url(image: bytes | str | Path, mime: str | None = None) -> str:
    if isinstance(image, (str, Path)):
        p = Path(image)
        data = p.read_bytes()
        if mime is None:
            ext = p.suffix.lower()
            mime = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/jpeg")
    else:
        data = image
    if mime is None:
        mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def user_message(text: str, images: Sequence[bytes | str | Path] = ()) -> dict:
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict] = [{"type": "image_url", "image_url": {"url": image_data_url(img)}} for img in images]
    parts.append({"type": "text", "text": text})
    return {"role": "user", "content": parts}


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json_loose(text: str) -> Any:
    """Parse JSON from model output: strips code fences, falls back to the
    outermost {...} span."""
    if text is None:
        raise ValueError("empty completion")
    s = text.strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if a != -1 and b > a:
            return json.loads(s[a:b + 1])
        raise


def validate(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Minimal JSON-schema validator for the subset we use (type, properties,
    required, items, enum, minimum, maximum, additionalProperties=false).
    Returns a list of error strings (empty = valid)."""
    errs: list[str] = []
    t = schema.get("type")
    types = t if isinstance(t, list) else ([t] if t else [])

    def is_type(v: Any, name: str) -> bool:
        return {
            "object": isinstance(v, dict),
            "array": isinstance(v, list),
            "string": isinstance(v, str),
            "integer": isinstance(v, int) and not isinstance(v, bool),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": isinstance(v, bool),
            "null": v is None,
        }.get(name, True)

    if types and not any(is_type(value, n) for n in types):
        return [f"{path}: expected {'|'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}.{req}: required")
        for k, v in value.items():
            if k in props:
                errs.extend(validate(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}.{k}: unexpected property")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs.extend(validate(v, schema["items"], f"{path}[{i}]"))
    return errs


def _is_disabled_status(status: Any) -> bool:
    if status is None:
        return False
    if isinstance(status, dict):
        status = status.get("status")
    if hasattr(status, "value"):  # Enum
        status = status.value
    return str(status).lower() in {"disabled", "quarantined"}


def _stage_paused(reason: str) -> Exception:
    try:
        from quill.stages import StagePaused  # type: ignore
    except Exception:  # pragma: no cover - only when A's module is missing
        return LLMError(reason)
    return StagePaused(reason)


def default_status_lookup(settings: Any) -> Callable[[str], Any]:
    """Lazy wrapper around quill.pipeline.catalog.model_status. Any failure
    (module missing, gateway unreachable) means "unknown" -> proceed."""

    def lookup(model: str) -> Any:
        try:
            from quill.pipeline import catalog  # type: ignore
        except Exception:
            return None
        fn = getattr(catalog, "model_status", None)
        if fn is None:
            return None
        for call in (lambda: fn(settings, model), lambda: fn(model)):
            try:
                return call()
            except TypeError:
                continue
            except Exception:
                return None
        return None

    return lookup


def urllib_transport(url: str, payload: dict, timeout: float) -> tuple[int, Any, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "quill/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        status = e.code
    try:
        body = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        body = None
    return status, body, raw


# ---------------------------------------------------------------- client

@dataclass
class ChatResult:
    content: str
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0
    finish_reason: str | None = None


class LLMClient:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_GATEWAY,
        *,
        timeout: float = 60.0,
        retries: int = 2,
        backoff: float = 2.0,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        transport: Transport | None = None,
        status_lookup: Callable[[str], Any] | None = None,
        status_ttl: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.transport = transport or default_transport or urllib_transport
        self.status_lookup = status_lookup
        self.status_ttl = status_ttl
        self.sleep = sleep
        self.schema_mode = "json_schema"  # downgraded to json_object if the gateway rejects it
        self._status_checked_at = -1e18
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Any, model_key: str = "TEXT_MODEL", **kw: Any) -> "LLMClient":
        kw.setdefault("status_lookup", default_status_lookup(settings))
        return cls(
            model=setting(settings, model_key, DEFAULT_MODEL),
            base_url=setting(settings, "GATEWAY_URL", DEFAULT_GATEWAY),
            **kw,
        )

    # -- catalog ---------------------------------------------------------
    def check_model_enabled(self, force: bool = False) -> None:
        if self.status_lookup is None:
            return
        now = time.monotonic()
        with self._lock:
            if not force and now - self._status_checked_at < self.status_ttl:
                return
            self._status_checked_at = now
        status = self.status_lookup(self.model)
        if _is_disabled_status(status):
            raise _stage_paused(f"{self.model} disabled in llm-proxy")

    # -- raw call --------------------------------------------------------
    def _post(self, payload: dict) -> ChatResult:
        url = f"{self.base_url}/chat/completions"
        last: str = ""
        for attempt in range(self.retries + 1):
            if attempt:
                self.sleep(self.backoff * (2 ** (attempt - 1)))
            t0 = time.monotonic()
            try:
                status, body, raw = self.transport(url, payload, self.timeout)
            except (TimeoutError, socket.timeout) as e:
                last = f"timeout after {self.timeout}s ({type(e).__name__})"
                continue
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                last = f"connection error: {type(e).__name__}: {getattr(e, 'reason', e)}"
                continue
            dt = time.monotonic() - t0
            if status == 200 and isinstance(body, dict):
                try:
                    choice = body["choices"][0]
                    content = choice["message"].get("content")
                except (KeyError, IndexError, TypeError, AttributeError):
                    last = "malformed completion body"
                    continue
                log.debug("llm ok model=%s latency=%.2fs usage=%s", self.model, dt, body.get("usage"))
                return ChatResult(content=content or "", usage=body.get("usage") or {},
                                  latency_s=dt, finish_reason=choice.get("finish_reason"))
            text = raw[:500] if raw else ""
            if status in (409, 403, 503) and re.search(r"disabled|quarantin", text, re.I):
                raise _stage_paused(f"{self.model} disabled in llm-proxy")
            if status == 400 and payload.get("response_format", {}).get("type") == "json_schema" \
                    and re.search(r"response_format|json_schema|guided|structured", text, re.I):
                raise _SchemaUnsupported(text)
            if status == 429 or status >= 500 or status == 200:
                last = f"HTTP {status}"
                continue
            raise LLMError(f"HTTP {status} from gateway: {text[:200]}")
        raise LLMError(f"LLM call failed after {self.retries + 1} attempts: {last}")

    def chat(
        self,
        messages: list[dict],
        *,
        response_format: dict | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking: bool = False,
    ) -> ChatResult:
        self.check_model_enabled()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
            "chat_template_kwargs": {"enable_thinking": bool(thinking)},
        }
        if response_format:
            payload["response_format"] = response_format
        return self._post(payload)

    # -- structured ------------------------------------------------------
    def chat_json(
        self,
        messages: list[dict],
        schema: dict,
        *,
        name: str = "result",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict:
        """Return a dict that validates against ``schema``.

        json_schema mode (enforced by the gateway) is tried first; on
        rejection falls back to json_object. Either way the result is
        validated locally and, if invalid, one repair round-trip is made."""
        if self.schema_mode == "json_schema":
            rf = {"type": "json_schema",
                  "json_schema": {"name": name, "strict": True, "schema": schema}}
        else:
            rf = {"type": "json_object"}
        msgs = list(messages)
        if self.schema_mode != "json_schema":
            msgs = _with_schema_hint(msgs, schema)
        try:
            res = self.chat(msgs, response_format=rf, max_tokens=max_tokens, temperature=temperature)
        except _SchemaUnsupported:
            log.warning("gateway rejected json_schema; falling back to json_object for %s", self.model)
            self.schema_mode = "json_object"
            return self.chat_json(messages, schema, name=name, max_tokens=max_tokens, temperature=temperature)

        errors: list[str]
        try:
            value = parse_json_loose(res.content)
            errors = validate(value, schema)
        except (ValueError, json.JSONDecodeError) as e:
            value, errors = None, [f"not valid JSON: {e}"]
        if not errors:
            return value
        # one repair retry
        repair = load_prompt("repair.txt").format(errors="\n".join(errors[:12]),
                                                  schema=json.dumps(schema))
        msgs2 = msgs + [{"role": "assistant", "content": res.content[:20000]},
                        {"role": "user", "content": repair}]
        res2 = self.chat(msgs2, response_format=rf, max_tokens=max_tokens, temperature=0.0)
        try:
            value = parse_json_loose(res2.content)
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMOutputError(f"invalid JSON after repair: {e}") from None
        errors = validate(value, schema)
        if errors:
            raise LLMOutputError("schema validation failed after repair: " + "; ".join(errors[:5]))
        return value


class _SchemaUnsupported(LLMError):
    pass


def _with_schema_hint(messages: list[dict], schema: dict) -> list[dict]:
    hint = ("Respond with a single JSON object only, no prose, matching this JSON schema:\n"
            + json.dumps(schema))
    out = list(messages)
    if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
        out[0] = {"role": "system", "content": out[0]["content"] + "\n\n" + hint}
    else:
        out.insert(0, {"role": "system", "content": hint})
    return out


# ---------------------------------------------------------------- timestamps

def fmt_ts(seconds: float) -> str:
    """mm:ss under an hour, h:mm:ss above (the format the prompts show)."""
    s = max(0, int(round(seconds or 0)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


_TS = re.compile(r"^\[?\s*(?:(\d+):)?(\d+):(\d{1,2}(?:\.\d+)?)\s*\]?$")


def parse_ts(value: Any) -> float | None:
    """Parse "mm:ss", "h:mm:ss", "[mm:ss]" or a number of seconds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    s = str(value).strip()
    m = _TS.match(s)
    if m:
        h = int(m.group(1) or 0)
        return h * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    try:
        v = float(s)
        return v if math.isfinite(v) else None
    except ValueError:
        return None


def render_lines(lines: Iterable[Any]) -> str:
    """Render transcript rows as ``[mm:ss] S2: text`` lines."""
    out = []
    for ln in lines:
        text = (ln["text"] or "").strip()
        if not text:
            continue
        out.append(f"[{fmt_ts(ln['start'])}] {ln['speaker'] or '?'}: {text}")
    return "\n".join(out)
