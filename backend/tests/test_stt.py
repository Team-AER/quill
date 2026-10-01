"""transcribe stage + Avifors jobs client against an in-process fake of the
llm-proxy/Avifors jobs API (httpx.MockTransport). Shapes mirror
llm/avifors/avifors/audio.py (Avifors 0.2.0)."""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fixtures.c1_helpers import FakeCtx, FakeSettings, make_beeps_opus, needs_ffmpeg  # noqa: E402

from quill.pipeline import catalog, stt  # noqa: E402
from quill.pipeline.media import Cancelled  # noqa: E402
from quill.pipeline.segments import Segment  # noqa: E402
from quill.stages import StageFailed, StagePaused  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled"}


class FakeAvifors:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.ids = itertools.count(1)
        self.catalog = {"aer-stt-v1": "ready", "aer-stt-qwen3": "ready"}
        self.polls_to_complete = 2
        self.submit_script: list[int] = []  # status codes to return before normal handling
        self.poll_script: dict[str, list[int]] = {}
        self.fail_job_times: dict[int, int] = {}  # clip idx -> times the job should fail
        self.permanent_idx: set[int] = set()
        self.on_submit = None
        self.on_poll = None
        self.requests: list[httpx.Request] = []
        self.submits = 0
        self.deletes: list[str] = []
        self.max_active = 0

    # -- helpers
    def view(self, j: dict) -> dict:
        return {
            "id": j["id"], "object": "audio.transcription.job", "model": j["model"],
            "engine_model": j["engine"], "status": j["status"], "created_at": 1.0,
            "duration": 5.0, "processed_seconds": 0.0, "progress": 0.0,
            "chunks_completed": 0, "chunks_total": 1, "error": j.get("error"),
            "expires_at": None, "result_url": f"/v1/audio/transcriptions/jobs/{j['id']}/result",
        }  # fmt: skip

    def err(self, status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message, "type": "avifors_error"}})

    def active(self) -> int:
        return sum(1 for j in self.jobs.values() if j["status"] not in TERMINAL)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/catalog.json":
            return httpx.Response(
                200, json={"models": [{"id": k, "status": v} for k, v in self.catalog.items()]}
            )
        m = re.fullmatch(r"/v1/audio/transcriptions/jobs(?:/(\w+))?(/result)?", path)
        assert m, path
        jid, is_result = m.group(1), bool(m.group(2))
        if request.method == "POST" and jid is None:
            return self.submit(request)
        j = self.jobs.get(jid)
        if j is None:
            return self.err(404, "unknown transcription job")
        if request.method == "DELETE":
            self.deletes.append(jid)
            if j["status"] not in ("completed", "cancelled"):
                j["status"] = "cancelled"
            return httpx.Response(200, json=self.view(j))
        if is_result:
            assert request.url.params.get("response_format") == "verbose_json"
            if j["status"] != "completed":
                return httpx.Response(409, json=self.view(j))
            return httpx.Response(200, json={
                "text": f"words {j['idx']} a words {j['idx']} b", "duration": 5.0,
                "segments": [{"id": 0, "start": 0.0, "end": 2.0, "text": f"words {j['idx']} a"},
                             {"id": 1, "start": 2.0, "end": 4.0, "text": f"words {j['idx']} b"},
                             {"id": 2, "start": 4.0, "end": 5.0, "text": ""}],
                "timestamp_granularity": "chunk", "language": j["language"],
            })  # fmt: skip
        if self.on_poll:
            self.on_poll(self)
        script = self.poll_script.get(jid)
        if script:
            return self.err(script.pop(0), "scripted")
        if j["status"] in ("queued", "running") and self.catalog.get(j["model"]) in ("ready", "degraded"):
            j["polls"] += 1
            j["status"] = "running"
            if j["polls"] >= self.polls_to_complete:
                if self.fail_job_times.get(j["idx"], 0) > 0:
                    self.fail_job_times[j["idx"]] -= 1
                    j["status"], j["error"] = "failed", "audio chunk failed repeatedly"
                else:
                    j["status"] = "completed"
        return httpx.Response(200, json=self.view(j))

    def submit(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Idempotency-Key")
        assert key and len(key) <= 256
        body = request.read()
        assert b'name="response_format"' in body and b"verbose_json" in body
        model = re.search(rb'name="model"\r\n\r\n([^\r]+)', body).group(1).decode()
        lang = re.search(rb'name="language"\r\n\r\n([^\r]+)', body)
        lang = lang.group(1).decode() if lang else None
        assert b'name="file"; filename="' in body and b"OggS" in body
        idx = int(key.split(":")[2])
        if key in self.by_key:
            return httpx.Response(200, json=self.view(self.jobs[self.by_key[key]]))
        self.submits += 1
        if self.on_submit:
            self.on_submit(self)
        if self.submit_script:
            code = self.submit_script.pop(0)
            if code == 503:  # Avifors keeps a failed row under the key
                self._new(key, model, lang, idx, status="failed", error="upload rejected or interrupted")
            return self.err(code, "transcription model disabled or proxy catalog unavailable"
                            if code == 503 else "scripted")
        if idx in self.permanent_idx:
            return self.err(400, "invalid audio or duration limit exceeded")
        if self.catalog.get(model) not in ("ready", "degraded"):
            self._new(key, model, lang, idx, status="failed", error="upload rejected or interrupted")
            return self.err(503, "transcription model disabled or proxy catalog unavailable")
        if self.active() >= 4:
            return self.err(429, "audio job quota exceeded")
        j = self._new(key, model, lang, idx)
        self.max_active = max(self.max_active, self.active())
        return httpx.Response(202, json=self.view(j))

    def _new(self, key, model, lang, idx, status="queued", error=None) -> dict:
        jid = f"{next(self.ids):032x}"
        engine = "aer-stt-qwen3" if lang == "en" and model == "aer-stt-v1" else model
        j = {"id": jid, "model": model, "engine": engine, "status": status, "error": error,
             "polls": 0, "language": lang, "idx": idx, "key": key}  # fmt: skip
        self.jobs[jid] = j
        self.by_key[key] = jid
        return j


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


TURNS = [("S1", 0, 8), ("S2", 8.5, 15), ("S1", 16, 25), ("S2", 26, 40), ("S1", 41, 55)]


@pytest.fixture(scope="session")
def opus_src(tmp_path_factory):
    return make_beeps_opus(tmp_path_factory.mktemp("stt") / "stt.opus", seconds=60)


@pytest.fixture
def env(tmp_path, opus_src, monkeypatch):
    fake = FakeAvifors()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(catalog, "transport", transport)
    monkeypatch.setattr(catalog, "CACHE_S", 0.0)
    catalog.clear_cache()
    settings = FakeSettings(data_dir=tmp_path, stt_concurrency=2)
    ctx = FakeCtx(tmp_path, settings=settings, language="en")
    ctx.exec("UPDATE meetings SET duration_s=60 WHERE id='m1'")
    (ctx.media_dir / "stt.opus").write_bytes(opus_src.read_bytes())
    set_turns(ctx, TURNS)
    clock = Clock()

    def run(**kw):
        client = stt.SttClient(settings, transport=transport)
        try:
            stt.run(ctx, client=client, sleep=clock.sleep, clock=clock, **kw)
        finally:
            client.close()

    yield fake, ctx, run, clock
    catalog.clear_cache()


def set_turns(ctx, turns):
    ctx.exec("DELETE FROM turns WHERE meeting_id='m1'")
    for s, a, b in turns:
        ctx.exec("INSERT INTO turns(meeting_id, speaker, start, end, overlap) VALUES ('m1',?,?,?,0)", s, a, b)


def seg_rows(ctx):
    return ctx.q("SELECT * FROM segments WHERE meeting_id='m1' ORDER BY idx")


def lines(ctx):
    return ctx.q("SELECT speaker, start, end, text, segment_id FROM transcript_lines ORDER BY start, id")


pytestmark = needs_ffmpeg


# ---------------------------------------------------------------- happy path


def test_transcribes_all_segments(env):
    fake, ctx, run, _ = env
    run()
    rows = seg_rows(ctx)
    assert len(rows) == 5 and all(r["status"] == "done" for r in rows)
    for r in rows:
        assert r["idempotency_key"] == f"quill:m1:{r['idx']}:{r['clip_sha256']}"
        assert r["engine_model"] == "aer-stt-qwen3"  # English hint routed to Qwen
        assert r["text"] == f"words {r['idx']} a words {r['idx']} b"
    ls = lines(ctx)
    assert len(ls) == 10  # two non-empty chunks per segment
    first = [l for l in ls if l["segment_id"] == rows[0]["id"]]
    assert first[0]["start"] == 0.0 and first[-1]["end"] == 8.0  # speech span, not padding
    assert first[1]["start"] == pytest.approx(2.0)  # clip start (pad clamped to 0) + chunk start
    second = [l for l in ls if l["segment_id"] == rows[1]["id"]]
    assert second[1]["start"] == pytest.approx(10.25)  # clip starts at 8.5 - 0.25 pad
    assert fake.max_active <= 2  # STT_CONCURRENCY
    assert ctx.progress_calls[-1][0] == 1.0
    # FTS (trigger-maintained) finds the lines
    hits = ctx.q("SELECT line_id FROM transcript_fts WHERE transcript_fts MATCH 'words' AND meeting_id='m1'")
    assert len(hits) == 10
    # language hint passed, transcript text never logged
    assert not any("words" in l for l in ctx.logs)
    assert fake.deletes == []


def test_rerun_reuses_done_segments(env):
    fake, ctx, run, _ = env
    run()
    before = [tuple(r) for r in lines(ctx)]
    n = fake.submits
    run()
    assert fake.submits == n
    after = lines(ctx)
    assert [tuple(r)[:4] for r in after] == [b[:4] for b in before]
    ids = {r["id"] for r in seg_rows(ctx)}
    assert {l["segment_id"] for l in after} <= ids


def test_changed_turns_only_retranscribe_changed_segments(env):
    fake, ctx, run, _ = env
    run()
    n = fake.submits
    set_turns(ctx, TURNS[:-1] + [("S1", 41, 50)])
    run()
    assert fake.submits == n + 1
    assert len(seg_rows(ctx)) == 5 and len(lines(ctx)) == 10


def test_no_turns(env):
    fake, ctx, run, _ = env
    set_turns(ctx, [])
    run()
    assert fake.submits == 0
    assert any("no speech" in r["message"] for r in ctx.q("SELECT message FROM events"))


def test_auto_language_omitted(env):
    fake, ctx, run, _ = env
    ctx.exec("UPDATE meetings SET language='auto'")
    run()
    body = fake.requests[[r.method for r in fake.requests].index("POST")].read()
    assert b'name="language"' not in body
    assert seg_rows(ctx)[0]["engine_model"] == "aer-stt-v1"


# ---------------------------------------------------------------- model controls


def test_paused_before_submit_when_disabled(env):
    fake, ctx, run, _ = env
    fake.catalog["aer-stt-v1"] = "disabled"
    with pytest.raises(StagePaused, match="STT model aer-stt-v1 is disabled in llm-proxy"):
        run()
    assert fake.submits == 0


def test_paused_when_routed_engine_quarantined(env):
    fake, ctx, run, _ = env
    del fake.catalog["aer-stt-qwen3"]
    with pytest.raises(StagePaused, match="aer-stt-qwen3 is missing"):
        run()


def test_catalog_unreachable_pauses(env, monkeypatch):
    fake, ctx, run, _ = env
    monkeypatch.setattr(catalog, "transport", httpx.MockTransport(lambda r: httpx.Response(502)))
    with pytest.raises(StagePaused, match="catalog unreachable"):
        run()


def test_disable_mid_run_pauses_keeps_jobs_and_resumes(env):
    fake, ctx, run, clock = env
    fake.polls_to_complete = 10**9  # nothing finishes before the admin disables the model
    fake.on_poll = lambda f: f.catalog.__setitem__("aer-stt-v1", "disabled")
    with pytest.raises(StagePaused, match="disabled"):
        run()
    rows = seg_rows(ctx)
    inflight = [r for r in rows if r["status"] == "submitted"]
    assert len(inflight) == 2 and all(r["stt_job_id"] for r in inflight)
    assert fake.deletes == []  # Avifors keeps their checkpoints
    assert lines(ctx) == []

    # admin re-enables; worker retries the stage
    fake.catalog["aer-stt-v1"] = "ready"
    fake.on_poll = None
    fake.polls_to_complete = 1
    job_ids = {r["stt_job_id"] for r in inflight}
    run()
    rows = seg_rows(ctx)
    assert all(r["status"] == "done" for r in rows)
    assert job_ids <= {r["stt_job_id"] for r in rows}  # re-polled, not resubmitted
    assert fake.submits == 5
    ls = lines(ctx)
    assert len(ls) == 10  # no duplicated transcript lines
    assert len({(l["start"], l["text"]) for l in ls}) == 10


def test_503_on_submit_pauses_then_new_key_on_resume(env):
    fake, ctx, run, _ = env
    fake.submit_script = [503]
    with pytest.raises(StagePaused):
        run()
    run()
    rows = seg_rows(ctx)
    assert all(r["status"] == "done" for r in rows)
    # the key Avifors holds as failed ("upload rejected") was replaced by :r1
    assert rows[0]["idempotency_key"].endswith(":r1")
    assert all(not r["idempotency_key"].endswith(":r1") for r in rows[1:])


def test_409_on_submit_pauses(env):
    fake, ctx, run, _ = env
    fake.submit_script = [409]
    with pytest.raises(StagePaused):
        run()


# ---------------------------------------------------------------- errors


def test_transient_errors_retry(env):
    fake, ctx, run, clock = env
    fake.submit_script = [502, 429, 507]
    run()
    assert all(r["status"] == "done" for r in seg_rows(ctx))
    jid = seg_rows(ctx)[0]["stt_job_id"]
    assert jid


def test_transient_poll_errors_retry(env):
    fake, ctx, run, _ = env
    fake.on_submit = lambda f: None
    run_first = {"done": False}

    orig = fake.submit

    def submit(request):
        resp = orig(request)
        if resp.status_code == 202 and not run_first["done"]:
            run_first["done"] = True
            fake.poll_script[resp.json()["id"]] = [500, 504]
        return resp

    fake.submit = submit
    run()
    assert all(r["status"] == "done" for r in seg_rows(ctx))


def test_permanent_failure_over_threshold_fails_stage(env):
    fake, ctx, run, _ = env
    fake.permanent_idx = {1}
    with pytest.raises(StageFailed, match="1 of 5 segments failed"):
        run()
    r = seg_rows(ctx)[1]
    assert r["status"] == "failed" and "400" in r["error"]


def test_permanent_failure_under_threshold_is_tolerated(env):
    fake, ctx, run, _ = env
    turns, t = [], 0.0
    for i in range(22):
        turns.append(("S1" if i % 2 == 0 else "S2", t, t + 2.0))
        t += 2.5
    set_turns(ctx, turns)
    fake.permanent_idx = {3}
    run()
    rows = seg_rows(ctx)
    assert len(rows) == 22
    assert sum(r["status"] == "failed" for r in rows) == 1
    assert any("1 of 22 segments failed" in r["message"] for r in ctx.q("SELECT message FROM events"))


def test_failed_job_resubmitted_with_new_key(env):
    fake, ctx, run, _ = env
    fake.fail_job_times = {2: 1}
    run()
    rows = seg_rows(ctx)
    assert rows[2]["status"] == "done" and rows[2]["idempotency_key"].endswith(":r1")


def test_job_failing_repeatedly_gives_up(env):
    fake, ctx, run, _ = env
    fake.fail_job_times = {2: 99}
    with pytest.raises(StageFailed):
        run()
    assert seg_rows(ctx)[2]["status"] == "failed"
    assert seg_rows(ctx)[2]["idempotency_key"].endswith(":r2")


def test_expired_job_404_resubmitted(env):
    fake, ctx, run, _ = env
    fake.polls_to_complete = 10**9
    fake.on_poll = lambda f: f.catalog.__setitem__("aer-stt-v1", "disabled")
    with pytest.raises(StagePaused):
        run()
    old = {r["stt_job_id"] for r in seg_rows(ctx) if r["stt_job_id"]}
    assert len(old) == 2
    # Avifors expired the jobs while Quill was paused
    fake.jobs.clear()
    fake.by_key.clear()
    fake.catalog["aer-stt-v1"] = "ready"
    fake.on_poll = None
    fake.polls_to_complete = 1
    run()
    rows = seg_rows(ctx)
    assert all(r["status"] == "done" for r in rows)
    assert not old & {r["stt_job_id"] for r in rows}
    assert sum(r["idempotency_key"].endswith(":r1") for r in rows) == 2


def test_auth_error_fails_stage(env):
    fake, ctx, run, _ = env
    fake.submit_script = [401]
    with pytest.raises(StageFailed, match="refused"):
        run()


def test_cancel_during_run(env):
    fake, ctx, run, _ = env
    fake.polls_to_complete = 10**9
    fake.on_submit = lambda f: setattr(ctx, "stop", True)
    with pytest.raises(Cancelled):
        run()


# ---------------------------------------------------------------- cancel_jobs


def test_cancel_jobs_deletes_inflight(env):
    fake, ctx, run, _ = env
    fake.polls_to_complete = 10**9
    fake.on_poll = lambda f: f.catalog.__setitem__("aer-stt-v1", "disabled")
    with pytest.raises(StagePaused):
        run()
    client = stt.SttClient(ctx.settings, transport=httpx.MockTransport(fake.handler))
    conn = ctx.db()
    try:
        n = stt.cancel_jobs(ctx.settings, "m1", conn=conn, client=client)
    finally:
        conn.close()
        client.close()
    assert n == 2 and len(fake.deletes) == 2
    assert all(fake.jobs[j]["status"] == "cancelled" for j in fake.deletes)
    assert {r["status"] for r in seg_rows(ctx)} == {"cancelled", "pending"}


def test_cancel_jobs_opens_its_own_db(env, monkeypatch):
    fake, ctx, run, _ = env
    ctx.exec(
        "INSERT INTO segments(meeting_id, idx, status, stt_job_id) VALUES ('m1', 0, 'submitted', 'deadbeef')"
    )
    monkeypatch.setattr(stt.SttClient, "__init__", _mock_init(fake))
    assert stt.cancel_jobs(ctx.settings, "m1") == 0  # 404 -> treated as gone, not counted


def _mock_init(fake):
    real = stt.SttClient.__init__

    def init(self, settings, *, transport=None, timeout=120.0):
        real(self, settings, transport=httpx.MockTransport(fake.handler), timeout=timeout)

    return init


# ---------------------------------------------------------------- units


def test_classify():
    def r(code, msg="x"):
        return httpx.Response(code, json={"error": {"message": msg, "type": "avifors_error"}})

    assert stt.classify(r(503)).kind == "pause"
    assert stt.classify(r(409)).kind == "pause"
    assert stt.classify(r(400, "model disabled")).kind == "pause"
    assert stt.classify(r(429)).kind == "transient"
    assert stt.classify(r(502)).kind == "transient"
    assert stt.classify(r(507)).kind == "transient"
    assert stt.classify(r(400)).kind == "permanent"
    assert stt.classify(r(413)).kind == "permanent"
    assert stt.classify(r(404)).kind == "gone"
    assert stt.classify(r(401)).kind == "auth"


def test_bump_key():
    k = stt.idempotency_key("m", 3, "ab")
    assert k == "quill:m:3:ab"
    assert stt._bump_key(k) == "quill:m:3:ab:r1"
    assert stt._bump_key("quill:m:3:ab:r1") == "quill:m:3:ab:r2"


def test_result_lines_clamping():
    seg = Segment("S1", start=9.75, end=20.25, speech_start=10.0, speech_end=20.0)
    ls = stt.result_lines(
        seg,
        {"text": "a b", "segments": [{"start": 0, "end": 6, "text": " a "}, {"start": 5.2, "end": 11, "text": "b"}]},
    )
    assert ls == [
        {"start": 10.0, "end": 15.75, "text": "a"},
        {"start": 15.75, "end": 20.0, "text": "b"},
    ]
    only_text = stt.result_lines(seg, {"text": "hello"})
    assert only_text == [{"start": 10.0, "end": 20.0, "text": "hello"}]
    assert stt.result_lines(seg, {"text": "", "segments": []}) == []


def test_normalize_language():
    assert stt.normalize_language("auto") is None
    assert stt.normalize_language("") is None
    assert stt.normalize_language(" hi ") == "hi"


def test_gateway_down_while_polling_pauses_instead_of_failing(env):
    fake, ctx, run, _ = env
    orig = fake.submit

    def submit(request):
        resp = orig(request)
        if resp.status_code == 202:
            fake.poll_script[resp.json()["id"]] = [502] * 50
        return resp

    fake.submit = submit
    with pytest.raises(StagePaused, match="unavailable"):
        run()
    assert all(r["status"] != "failed" for r in seg_rows(ctx))
    assert fake.deletes == []


def test_network_down_on_submit_pauses(env, monkeypatch):
    fake, ctx, run, _ = env

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(fake, "submit", refuse)
    with pytest.raises(StagePaused, match="network error"):
        run()
