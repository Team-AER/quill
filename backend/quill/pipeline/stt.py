"""Stage ``transcribe``: per-speaker-segment STT through the llm-proxy durable
jobs API (Avifors, ``aer-stt-v1``). PLAN §4.4.

Gateway API (Avifors 0.2.0 ``avifors/audio.py``, verified live 2026-09-30)
--------------------------------------------------------------------------
``POST {GATEWAY}/audio/transcriptions/jobs`` multipart ``model``, ``file``,
optional ``language`` (ISO 639 or ``xxx_Scrp``), ``response_format``; header
``Idempotency-Key`` (<= 256 chars).
  * 202 + job view: new job.
  * 200 + job view: the key already exists for this identity -> that job is
    returned *whatever its state* (the upload is ignored). A job whose upload was
    rejected (e.g. 503 while the model was disabled) stays ``failed`` under that
    key, so after such a rejection we must use a new key (``...:r<n>``).
  * errors: ``{"error": {"message", "type": "avifors_error"}}``;
    400 bad field/audio, 403/404 model, 409 cancelled upload, 413 too big,
    429 quota (4 active jobs per identity -- the LAN shares one identity!),
    503 model disabled / catalog unavailable (Retry-After: 5), 507 storage.
``GET .../jobs/{id}`` -> job view::

    {"id", "object": "audio.transcription.job", "model", "engine_model",
     "status": "uploading|uploaded|queued|running|completed|failed|cancelled",
     "created_at", "duration", "processed_seconds", "progress",
     "chunks_completed", "chunks_total", "error", "expires_at", "result_url"}

  A disabled model does *not* surface as a state: the job just stays
  ``queued`` at a chunk boundary and resumes when re-enabled.
``GET .../jobs/{id}/result?response_format=verbose_json`` -> 200
  ``{"text", "duration", "segments": [{"id", "start", "end", "text"}],
  "timestamp_granularity": "chunk", "language"}``; 409 + job view if not done.
``DELETE .../jobs/{id}`` -> 200 job view (cancelled); 404 if unknown/expired.

Policy
------
* Model checks: catalog (``catalog.model_status``) before submitting and every
  30 s. Not ready/degraded -> ``StagePaused``; in-flight jobs are left alone so
  Avifors keeps their checkpoints, and are re-polled on resume (their
  ``stt_job_id`` is persisted as soon as they are accepted).
* 409/503 or a "disabled" message on submit -> pause. 429/507/5xx/network ->
  transient, retried with backoff. Other 4xx -> the segment fails. The stage
  fails when more than 5 % of segments failed.
* Never log transcript text.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from quill.pipeline import catalog
from quill.pipeline.catalog import setting
from quill.pipeline.media import Cancelled, cut_clip
from quill.pipeline.segments import Segment, build_segments
from quill.stages import StageFailed, StagePaused

JOBS_PATH = "audio/transcriptions/jobs"
CATALOG_EVERY_S = 30.0
FAIL_RATIO = 0.05
MAX_JOB_ATTEMPTS = 3  # failed/expired jobs resubmitted with a new key
MAX_TRANSIENT = 8  # consecutive transient HTTP errors per segment
POLL_MIN_S, POLL_MAX_S = 1.0, 10.0
# Avifors' administrator routing for the public alias (examples/stt.yaml,
# deployed config): English hints are served by the Qwen engine, and Avifors
# requires both alias and engine to be enabled. Job views report the real
# engine_model, which we also watch once known.
ENGINE_HINTS = {"aer-stt-v1": {"en": "aer-stt-qwen3", "eng": "aer-stt-qwen3", "eng_Latn": "aer-stt-qwen3"}}
TERMINAL = {"completed", "failed", "cancelled"}


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


# ================================================================ HTTP client


class SttError(Exception):
    """kind: "pause" | "transient" | "permanent" | "gone" | "auth"."""

    def __init__(self, kind: str, status: int | None, message: str) -> None:
        super().__init__(f"{kind} {status}: {message}")
        self.kind, self.status, self.message = kind, status, message


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:200]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or "")[:200]
        if isinstance(err, str):
            return err[:200]
        if body.get("status") and body.get("error") is None:
            return f"job {body.get('status')}"
    return str(body)[:200]


def classify(response: httpx.Response) -> SttError:
    status = response.status_code
    message = _error_message(response)
    if status in (409, 503) or "disabled" in message.lower():
        return SttError("pause", status, message)
    if status in (401, 403):
        return SttError("auth", status, message)
    if status == 404:
        return SttError("gone", status, message)
    if status in (408, 425, 429, 507) or status >= 500:
        return SttError("transient", status, message)
    return SttError("permanent", status, message)


class SttClient:
    """Thin sync client for the Avifors durable jobs API via llm-proxy."""

    def __init__(
        self,
        settings: Any,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        base = str(setting(settings, "GATEWAY_URL", "http://localhost:4000/v1")).rstrip("/") + "/"
        headers = {"User-Agent": "quill/1"}
        key = setting(settings, "STT_API_KEY") or setting(settings, "GATEWAY_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        self.http = httpx.Client(
            base_url=base,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "SttClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _call(self, method: str, url: str, **kw: Any) -> httpx.Response:
        try:
            return self.http.request(method, url, **kw)
        except httpx.TransportError as exc:
            raise SttError("transient", None, type(exc).__name__) from exc

    def submit(
        self, clip: Path, *, model: str, language: str | None, key: str
    ) -> tuple[dict[str, Any], bool]:
        """Returns (job_view, fresh). fresh=False when the key already existed."""
        data = {"model": model, "response_format": "verbose_json"}
        if language:
            data["language"] = language
        with open(clip, "rb") as fh:
            r = self._call(
                "POST",
                JOBS_PATH,
                data=data,
                files={"file": (clip.name, fh, "audio/ogg")},
                headers={"Idempotency-Key": key},
            )
        if r.status_code in (200, 201, 202):
            return r.json(), r.status_code != 200
        raise classify(r)

    def job(self, job_id: str) -> dict[str, Any]:
        r = self._call("GET", f"{JOBS_PATH}/{job_id}", headers={"Cache-Control": "no-cache"})
        if r.status_code == 200:
            return r.json()
        raise classify(r)

    def result(self, job_id: str) -> dict[str, Any] | None:
        """verbose_json result, or None while the job is not completed (409)."""
        r = self._call("GET", f"{JOBS_PATH}/{job_id}/result", params={"response_format": "verbose_json"})
        if r.status_code == 200:
            return r.json()
        if r.status_code == 409:
            return None
        raise classify(r)

    def delete(self, job_id: str) -> dict[str, Any] | None:
        r = self._call("DELETE", f"{JOBS_PATH}/{job_id}")
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        raise classify(r)


# ================================================================ helpers


def idempotency_key(meeting_id: str, idx: int, sha256: str, rev: int = 0) -> str:
    base = f"quill:{meeting_id}:{idx}:{sha256}"
    return base if rev == 0 else f"{base}:r{rev}"


_REV = re.compile(r"^(.*):r(\d+)$")


def _bump_key(key: str) -> str:
    """quill:m:i:sha -> quill:m:i:sha:r1 -> ...:r2 (a new Avifors job)."""
    m = _REV.match(key)
    return f"{m.group(1)}:r{int(m.group(2)) + 1}" if m else f"{key}:r1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def normalize_language(value: Any) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return None if not v or v.lower() in ("auto", "none", "detect") else v


def result_lines(seg: Segment, result: dict[str, Any]) -> list[dict[str, Any]]:
    """verbose_json -> transcript lines (one per STT chunk; times absolute and
    clamped to the segment's speech span)."""
    lo, hi = seg.speech_start, max(seg.speech_end, seg.speech_start)
    lines: list[dict[str, Any]] = []
    chunks = result.get("segments") or []
    for c in chunks:
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        try:
            a = seg.start + float(c.get("start", 0.0))
            b = seg.start + float(c.get("end", 0.0))
        except (TypeError, ValueError):
            a, b = lo, hi
        a = min(max(a, lo), hi)
        b = min(max(b, a), hi)
        if lines and a < lines[-1]["end"]:
            a = lines[-1]["end"] if lines[-1]["end"] <= b else a
        lines.append({"start": round(a, 3), "end": round(b, 3), "text": text})
    if not lines:
        text = str(result.get("text") or "").strip()
        if text:
            lines.append({"start": lo, "end": hi, "text": text})
    if lines:  # the first/last line cover the whole speech span
        lines[0]["start"] = lo
        lines[-1]["end"] = hi
    return lines


# ---------------------------------------------------------------- FTS


def _fts_mode(conn: sqlite3.Connection) -> str:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='transcript_fts'"
    ).fetchone()
    if not has_table:
        return "none"
    trig = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='transcript_lines'"
    ).fetchone()
    return "trigger" if trig else "manual"


def _delete_lines(conn: sqlite3.Connection, where: str, args: tuple, fts: str) -> None:
    if fts == "manual":
        ids = [r[0] for r in conn.execute(f"SELECT id FROM transcript_lines WHERE {where}", args)]
        if ids:
            try:
                conn.executemany("DELETE FROM transcript_fts WHERE line_id=?", [(i,) for i in ids])
            except sqlite3.OperationalError:
                pass
    conn.execute(f"DELETE FROM transcript_lines WHERE {where}", args)


def _insert_lines(
    conn: sqlite3.Connection, meeting_id: str, seg: Segment, segment_id: int, lines: list[dict], fts: str
) -> None:
    for line in lines:
        cur = conn.execute(
            "INSERT INTO transcript_lines(meeting_id, speaker, start, end, text, overlap, segment_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (meeting_id, seg.speaker, line["start"], line["end"], line["text"], 1 if seg.overlap else 0, segment_id),
        )
        if fts == "manual":
            conn.execute(
                "INSERT INTO transcript_fts(text, meeting_id, line_id) VALUES (?,?,?)",
                (line["text"], meeting_id, cur.lastrowid),
            )


# ================================================================ stage


@dataclass
class Work:
    seg: Segment
    clip: Path
    sha: str
    row_id: int = 0
    key: str = ""
    status: str = "pending"  # pending | submitted | done | failed
    job_id: str | None = None
    engine: str | None = None
    attempts: int = 0  # failed/expired jobs
    transient: int = 0  # consecutive transient errors
    not_before: float = 0.0
    next_poll: float = 0.0
    poll_every: float = POLL_MIN_S
    error: str | None = None
    extra: dict = field(default_factory=dict)


def _clip_name(seg: Segment) -> str:
    return f"{seg.idx:05d}_{int(round(seg.start * 1000))}_{int(round(seg.end * 1000))}.opus"


def prepare_clips(ctx: Any, segs: list[Segment]) -> list[Work]:
    media_dir = Path(ctx.media_dir)
    opus = media_dir / "stt.opus"
    if not opus.exists():
        raise StageFailed("stt.opus is missing; re-run audio extraction")
    clips = media_dir / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    wanted = {_clip_name(s) for s in segs}
    for stale in clips.glob("*.opus"):
        if stale.name not in wanted:
            stale.unlink(missing_ok=True)
    works = []
    for n, seg in enumerate(segs):
        if ctx.should_stop():
            raise Cancelled()
        path = clips / _clip_name(seg)
        if not path.exists():
            cut_clip(opus, seg.start, seg.end, path)
        works.append(Work(seg=seg, clip=path, sha=sha256_file(path)))
        if n % 25 == 0:
            ctx.progress(0.0, f"preparing clips {n}/{len(segs)}")
    return works


def reconcile(ctx: Any, works: list[Work], client: SttClient | None) -> None:
    """Replace segment rows, carrying over finished and in-flight work whose
    (speaker, span, clip sha256) is unchanged."""

    def k(speaker, start, end, sha):
        return (speaker, round(float(start), 3), round(float(end), 3), sha)

    conn = ctx.db()
    try:
        old = {
            k(r["speaker"], r["start"], r["end"], r["clip_sha256"]): r
            for r in conn.execute("SELECT * FROM segments WHERE meeting_id=?", (ctx.meeting_id,))
        }
        stale_jobs: list[str] = []
        used: set = set()
        for w in works:
            r = old.get(k(w.seg.speaker, w.seg.start, w.seg.end, w.sha))
            w.key = idempotency_key(ctx.meeting_id, w.seg.idx, w.sha)
            if r is None:
                continue
            used.add(k(r["speaker"], r["start"], r["end"], r["clip_sha256"]))
            w.extra["old_id"] = r["id"]
            if r["status"] == "done":
                w.status, w.key = "done", r["idempotency_key"] or w.key
                w.engine, w.job_id = r["engine_model"], r["stt_job_id"]
                w.extra["text"] = r["text"]
            elif r["status"] == "submitted" and r["stt_job_id"]:
                w.status, w.job_id = "submitted", r["stt_job_id"]
                w.key = r["idempotency_key"] or w.key
                w.engine = r["engine_model"]
            elif r["idempotency_key"]:
                # Keep the key revision; a failed/cancelled job under the old key
                # must not be reused (Avifors returns it as-is for that key).
                w.key = r["idempotency_key"]
                if r["status"] in ("failed", "cancelled"):
                    w.key = _bump_key(w.key)
        for key_, r in old.items():
            if key_ not in used and r["status"] == "submitted" and r["stt_job_id"]:
                stale_jobs.append(r["stt_job_id"])
        fts = _fts_mode(conn)
        with conn:
            keep_old_ids = [w.extra["old_id"] for w in works if w.status == "done" and "old_id" in w.extra]
            if keep_old_ids:
                marks = ",".join("?" * len(keep_old_ids))
                _delete_lines(
                    conn,
                    f"meeting_id=? AND (segment_id IS NULL OR segment_id NOT IN ({marks}))",
                    (ctx.meeting_id, *keep_old_ids),
                    fts,
                )
            else:
                _delete_lines(conn, "meeting_id=?", (ctx.meeting_id,), fts)
            conn.execute("DELETE FROM segments WHERE meeting_id=?", (ctx.meeting_id,))
            for w in works:
                cur = conn.execute(
                    "INSERT INTO segments(meeting_id, idx, speaker, start, end, clip_sha256, "
                    "idempotency_key, stt_job_id, status, text, engine_model, error, interjections) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ctx.meeting_id, w.seg.idx, w.seg.speaker, w.seg.start, w.seg.end, w.sha,
                        w.key, w.job_id, w.status, w.extra.get("text"), w.engine, None,
                        json.dumps(w.seg.interjections),
                    ),
                )  # fmt: skip
                w.row_id = cur.lastrowid
                if w.status == "done" and "old_id" in w.extra:
                    conn.execute(
                        "UPDATE transcript_lines SET segment_id=? WHERE meeting_id=? AND segment_id=?",
                        (-w.row_id, ctx.meeting_id, w.extra["old_id"]),
                    )
            # two-step remap avoids collisions between old and new ids
            conn.execute(
                "UPDATE transcript_lines SET segment_id=-segment_id WHERE meeting_id=? AND segment_id<0",
                (ctx.meeting_id,),
            )
    finally:
        conn.close()
    if stale_jobs and client is not None:
        for jid in stale_jobs:
            try:
                client.delete(jid)
            except SttError:
                pass


def _save(ctx: Any, w: Work, **cols: Any) -> None:
    conn = ctx.db()
    try:
        with conn:
            sets = ",".join(f"{c}=?" for c in cols)
            conn.execute(f"UPDATE segments SET {sets} WHERE id=?", (*cols.values(), w.row_id))
    finally:
        conn.close()


def _complete(ctx: Any, w: Work, result: dict[str, Any], engine: str | None) -> None:
    lines = result_lines(w.seg, result)
    text = " ".join(l["text"] for l in lines)
    conn = ctx.db()
    try:
        fts = _fts_mode(conn)
        with conn:
            _delete_lines(conn, "meeting_id=? AND segment_id=?", (ctx.meeting_id, w.row_id), fts)
            _insert_lines(conn, ctx.meeting_id, w.seg, w.row_id, lines, fts)
            conn.execute(
                "UPDATE segments SET status='done', text=?, engine_model=?, error=NULL, stt_job_id=? WHERE id=?",
                (text, engine, w.job_id, w.row_id),
            )
    finally:
        conn.close()
    w.status, w.engine = "done", engine


def _event(ctx: Any, kind: str, message: str) -> None:
    conn = ctx.db()
    try:
        with conn:
            conn.execute(
                "INSERT INTO events(meeting_id, at, kind, message) VALUES (?,?,?,?)",
                (ctx.meeting_id, _now(), kind, message[:1000]),
            )
    finally:
        conn.close()


def check_models(settings: Any, models: set[str], *, force: bool = False) -> None:
    """Raise StagePaused unless every model is ready/degraded in the catalog."""
    for mid in sorted(models):
        status = catalog.model_status(settings, mid, force=force)
        if catalog.is_usable(status):
            continue
        if status == "unknown":
            raise StagePaused(f"STT paused: llm-proxy catalog unreachable (model {mid})")
        if status == "missing":
            raise StagePaused(f"STT model {mid} is missing from llm-proxy")
        raise StagePaused(f"STT model {mid} is {status} in llm-proxy")


def run(
    ctx: Any,
    *,
    client: SttClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    settings = ctx.settings
    model = str(setting(settings, "STT_MODEL", "aer-stt-v1"))
    concurrency = max(1, int(setting(settings, "STT_CONCURRENCY", 3)))

    conn = ctx.db()
    try:
        meeting = conn.execute(
            "SELECT language, duration_s FROM meetings WHERE id=?", (ctx.meeting_id,)
        ).fetchone()
        if meeting is None:
            raise StageFailed("meeting not found")
        turns = conn.execute(
            "SELECT speaker, start, end, overlap FROM turns WHERE meeting_id=? ORDER BY start",
            (ctx.meeting_id,),
        ).fetchall()
    finally:
        conn.close()
    language = normalize_language(meeting["language"])
    segs = build_segments(turns, meeting["duration_s"])
    watch = {model}
    hint = ENGINE_HINTS.get(model, {}).get(language or "")
    if hint:
        watch.add(hint)

    own_client = client is None
    if own_client:
        client = SttClient(settings)
    try:
        works = prepare_clips(ctx, segs)
        reconcile(ctx, works, client)
        total = len(works)
        if total == 0:
            _event(ctx, "warning", "transcribe: no speech turns to transcribe")
            ctx.progress(1.0, "no speech")
            return
        todo = [w for w in works if w.status != "done"]
        if todo:
            check_models(settings, watch | {w.engine for w in todo if w.engine}, force=True)
        _drive(ctx, client, works, model, language, concurrency, watch, sleep, clock)
    finally:
        if own_client:
            client.close()

    failed = [w for w in works if w.status == "failed"]
    if failed:
        _event(ctx, "warning", f"transcribe: {len(failed)} of {total} segments failed")
    if len(failed) > FAIL_RATIO * total:
        raise StageFailed(f"{len(failed)} of {total} segments failed to transcribe")
    ctx.log(f"transcribe: {total - len(failed)}/{total} segments done")
    ctx.progress(1.0, f"{total - len(failed)}/{total} segments")


def _drive(
    ctx: Any,
    client: SttClient,
    works: list[Work],
    model: str,
    language: str | None,
    concurrency: int,
    watch: set[str],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    total = len(works)
    pending = [w for w in works if w.status == "pending"]
    inflight = [w for w in works if w.status == "submitted"]
    last_catalog = clock()
    last_reported = -1

    def report() -> None:
        nonlocal last_reported
        finished = sum(1 for w in works if w.status in ("done", "failed"))
        if finished != last_reported:
            last_reported = finished
            ctx.progress(finished / total, f"{finished}/{total} segments")

    def fail(w: Work, message: str) -> None:
        w.status, w.error = "failed", message
        _save(ctx, w, status="failed", error=message[:300])
        ctx.log(f"transcribe: segment {w.seg.idx} failed: {message[:120]}")

    def resubmit(w: Work, reason: str, counts: bool = True) -> None:
        if counts:
            w.attempts += 1
            if w.attempts >= MAX_JOB_ATTEMPTS:
                if w in inflight:
                    inflight.remove(w)
                fail(w, reason)
                return
        w.key, w.job_id, w.status = _bump_key(w.key), None, "pending"
        _save(ctx, w, status="pending", stt_job_id=None, idempotency_key=w.key)
        if w in inflight:
            inflight.remove(w)
        pending.insert(0, w)

    def transient(w: Work, err: SttError, now: float) -> bool:
        """Back off; returns False when the segment gave up."""
        if err.status != 429:  # quota waits never count as failures
            w.transient += 1
        if w.transient > MAX_TRANSIENT:
            # Gateway unreachable / upstream down, or polling an accepted job:
            # pause (Avifors keeps the work). Anything else is input-specific.
            if w in inflight or err.status in (None, 502, 503, 504):
                raise StagePaused(f"llm-proxy STT unavailable ({err.status or 'network error'})")
            fail(w, f"gateway error {err.status}: {err.message}")
            return False
        delay = min(60.0, 2.0 ** min(w.transient + 1, 6))
        w.not_before = w.next_poll = now + delay
        return True

    def handle_view(w: Work, view: dict[str, Any], now: float) -> None:
        state = str(view.get("status") or "")
        if view.get("engine_model"):
            w.engine = str(view["engine_model"])
            watch.add(w.engine)
        if state == "completed":
            result = client.result(w.job_id)
            if result is None:  # raced; poll again soon
                w.next_poll = now + POLL_MIN_S
                return
            _complete(ctx, w, result, w.engine or view.get("model"))
            inflight.remove(w)
        elif state == "failed":
            err = str(view.get("error") or "job failed")
            if "upload rejected or interrupted" in err:
                resubmit(w, err, counts=False)  # rejected while paused/interrupted
            elif "invalid audio" in err:
                inflight.remove(w)
                fail(w, err)
            else:
                resubmit(w, err)
        elif state == "cancelled":
            if ctx.should_stop():
                raise Cancelled()
            resubmit(w, "job cancelled externally")
        else:
            w.poll_every = min(POLL_MAX_S, w.poll_every * 1.5)
            w.next_poll = now + w.poll_every

    report()
    while pending or inflight:
        if ctx.should_stop():
            raise Cancelled()
        now = clock()
        if now - last_catalog >= CATALOG_EVERY_S:
            last_catalog = now
            check_models(ctx.settings, watch)  # raises StagePaused; in-flight jobs keep checkpoints

        # --- submit new work
        submitted_any = True
        while submitted_any and pending and len(inflight) < concurrency:
            submitted_any = False
            ready = [w for w in pending if w.not_before <= now]
            if not ready:
                break
            w = ready[0]
            try:
                view, fresh = client.submit(w.clip, model=model, language=language, key=w.key)
            except SttError as err:
                if err.kind == "pause":
                    raise StagePaused(
                        f"STT model {model} is disabled in llm-proxy ({err.status})"
                    ) from None
                if err.kind == "auth":
                    raise StageFailed(f"llm-proxy refused STT access ({err.status})") from None
                if err.kind == "transient":
                    transient(w, err, now)
                    if w.status == "failed":
                        pending.remove(w)
                    break  # back off the whole submit loop this tick
                pending.remove(w)
                fail(w, f"rejected ({err.status}): {err.message}")
                report()
                submitted_any = True
                continue
            pending.remove(w)
            w.transient = 0
            w.job_id, w.status = str(view["id"]), "submitted"
            w.poll_every, w.next_poll = POLL_MIN_S, now + POLL_MIN_S
            _save(ctx, w, status="submitted", stt_job_id=w.job_id, idempotency_key=w.key, error=None)
            inflight.append(w)
            if not fresh:
                handle_view(w, view, now)  # existing job for this key: act on its state now
                report()
            submitted_any = True

        # --- poll in-flight jobs that are due
        for w in [w for w in inflight if w.next_poll <= now]:
            try:
                view = client.job(w.job_id)
                handle_view(w, view, now)
                w.transient = 0
            except SttError as err:
                if err.kind == "gone":
                    resubmit(w, "job expired or unknown")
                elif err.kind == "auth":
                    raise StageFailed(f"llm-proxy refused STT access ({err.status})") from None
                elif err.kind in ("transient", "pause"):
                    transient(w, err, now)
                else:
                    inflight.remove(w)
                    fail(w, f"poll rejected ({err.status}): {err.message}")
            report()

        if not (pending or inflight):
            break
        due = [w.next_poll for w in inflight] + [w.not_before for w in pending if len(inflight) < concurrency]
        wait = (min(due) - clock()) if due else POLL_MIN_S
        sleep(min(1.0, max(0.05, wait)))
    report()


# ================================================================ cancellation


def cancel_jobs(
    settings: Any,
    meeting_id: str,
    *,
    conn: sqlite3.Connection | None = None,
    client: SttClient | None = None,
) -> int:
    """DELETE every in-flight Avifors job of a meeting (meeting deleted or
    cancelled). Returns how many jobs were cancelled. Best effort."""
    own_conn = conn is None
    if own_conn:
        from quill import db as qdb

        data_dir = Path(str(setting(settings, "DATA_DIR", "/var/lib/quill")))
        conn = qdb.connect(data_dir / "db" / "quill.sqlite3")
    own_client = client is None
    if own_client:
        client = SttClient(settings, timeout=15.0)
    cancelled = 0
    try:
        rows = conn.execute(
            "SELECT id, stt_job_id FROM segments WHERE meeting_id=? AND status='submitted' "
            "AND stt_job_id IS NOT NULL",
            (meeting_id,),
        ).fetchall()
        for row in rows:
            try:
                if client.delete(row[1]) is not None:
                    cancelled += 1
            except SttError:
                continue
            try:
                with conn:
                    conn.execute(
                        "UPDATE segments SET status='cancelled' WHERE id=?", (row[0],)
                    )
            except sqlite3.Error:
                pass
    finally:
        if own_client:
            client.close()
        if own_conn:
            conn.close()
    return cancelled
