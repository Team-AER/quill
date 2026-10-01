import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

import quill.pipeline
from quill import db, stages, worker
from quill.config import Settings
from quill.stages import STAGES, StageFailed, StagePaused


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path)
    s.ensure_dirs()
    db.init(s.db_path)
    # never talk to a real STT backend from worker tests
    fake_stt = types.ModuleType("quill.pipeline.stt")
    fake_stt.cancelled = []
    fake_stt.cancel_jobs = lambda settings, mid: fake_stt.cancelled.append(mid)
    monkeypatch.setitem(sys.modules, "quill.pipeline.stt", fake_stt)
    monkeypatch.setattr(quill.pipeline, "stt", fake_stt, raising=False)
    return s


def new_meeting(settings, mode="video", title="m"):
    with db.opened(settings.db_path) as c:
        mid = db.create_meeting(c, owner_id=1, title=title, mode=mode)
    settings.media_dir(mid).mkdir(parents=True, exist_ok=True)
    return mid


def stage_rows(settings, mid):
    with db.opened(settings.db_path) as c:
        return {r["name"]: dict(r) for r in c.execute("SELECT * FROM stages WHERE meeting_id=?", (mid,))}


def meeting(settings, mid):
    with db.opened(settings.db_path) as c:
        r = c.execute("SELECT * FROM meetings WHERE id=?", (mid,)).fetchone()
        return dict(r) if r else None


class Recorder:
    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    def runners(self):
        def make(name):
            def run(ctx):
                self.calls.append(name)
                ctx.progress(0.5, "halfway")
                if name in self.overrides:
                    self.overrides[name](ctx)
            return run
        return {name: make(name) for name in STAGES}


def test_happy_path_video(settings):
    mid = new_meeting(settings)
    rec = Recorder()
    w = worker.Worker(settings, runners=rec.runners())
    assert w.run_once() is True
    assert rec.calls == STAGES
    rows = stage_rows(settings, mid)
    assert all(r["status"] == "done" and r["progress"] == 1 and r["finished_at"] for r in rows.values())
    assert meeting(settings, mid)["status"] == "done"
    assert w.run_once() is False  # nothing left


def test_progress_is_written(settings):
    mid = new_meeting(settings)
    seen = {}

    def probe(ctx):
        ctx.progress(0.25, "reading streams")
        seen.update(stage_rows(settings, mid)["probe"])
        assert ctx.stage == "probe" and ctx.mode == "video" and ctx.media_dir == settings.media_dir(mid)
        ctx.log("probe ok")

    rec = Recorder({"probe": probe})
    worker.Worker(settings, runners=rec.runners()).run_once()
    assert seen["status"] == "running" and seen["progress"] == 0.25 and seen["detail"] == "reading streams"
    with db.opened(settings.db_path) as c:
        assert c.execute("SELECT count(*) FROM events WHERE kind='log' AND message LIKE '%probe ok%'").fetchone()[0] == 1


def test_audio_mode_skips_video_stages(settings):
    mid = new_meeting(settings, mode="audio")
    rec = Recorder()
    worker.Worker(settings, runners=rec.runners()).run_once()
    assert rec.calls == ["probe", "extract_audio", "diarize", "transcribe", "synthesize"]
    rows = stage_rows(settings, mid)
    assert rows["key_moments"]["status"] == rows["frames"]["status"] == "skipped"
    assert meeting(settings, mid)["status"] == "done"


def test_probe_can_switch_to_audio(settings):
    mid = new_meeting(settings, mode="video")

    def probe(ctx):
        with ctx.db() as c:
            c.execute("UPDATE meetings SET mode='audio', has_video=0 WHERE id=?", (ctx.meeting_id,))

    rec = Recorder({"probe": probe})
    worker.Worker(settings, runners=rec.runners()).run_once()
    assert "frames" not in rec.calls and "key_moments" not in rec.calls
    assert meeting(settings, mid)["status"] == "done"


def test_pause_and_retry(settings):
    mid = new_meeting(settings)
    state = {"paused": True}

    def transcribe(ctx):
        if state["paused"]:
            raise StagePaused("STT model disabled in llm-proxy")

    rec = Recorder({"transcribe": transcribe})
    w = worker.Worker(settings, runners=rec.runners(), pause_retry=3600)
    w.run_once()
    rows = stage_rows(settings, mid)
    assert rows["transcribe"]["status"] == "paused"
    assert rows["transcribe"]["detail"] == "STT model disabled in llm-proxy"
    assert rows["diarize"]["status"] == "done" and rows["synthesize"]["status"] == "pending"
    assert meeting(settings, mid)["status"] == "paused"
    # not retried before the retry time
    assert w.next_meeting() is None
    assert w.run_once() is False
    # after the retry time, it resumes at the paused stage only
    w._retry_at[mid] = 0
    state["paused"] = False
    rec.calls.clear()
    w.run_once()
    assert rec.calls == ["transcribe", "key_moments", "frames", "synthesize"]
    assert meeting(settings, mid)["status"] == "done"


def test_paused_meeting_does_not_block_others(settings):
    a = new_meeting(settings, title="a")
    b = new_meeting(settings, title="b")

    def transcribe(ctx):
        if ctx.meeting_id == a:
            raise StagePaused("busy")

    w = worker.Worker(settings, runners=Recorder({"transcribe": transcribe}).runners(), pause_retry=3600)
    w.run_once()
    assert meeting(settings, a)["status"] == "paused"
    w.run_once()
    assert meeting(settings, b)["status"] == "done"


def test_stage_failed_then_rerun(settings):
    mid = new_meeting(settings)
    state = {"fail": True}

    def diarize(ctx):
        if state["fail"]:
            raise StageFailed("diarizer exited 1")

    rec = Recorder({"diarize": diarize})
    w = worker.Worker(settings, runners=rec.runners())
    w.run_once()
    rows = stage_rows(settings, mid)
    assert rows["diarize"]["status"] == "failed" and rows["diarize"]["error"] == "diarizer exited 1"
    m = meeting(settings, mid)
    assert m["status"] == "failed" and m["error"] == "diarize: diarizer exited 1"
    assert w.run_once() is False  # failed meetings are not picked up
    # rerun as the API does it
    with db.opened(settings.db_path) as c:
        for name in STAGES[STAGES.index("diarize"):]:
            c.execute("UPDATE stages SET status='pending', error=NULL WHERE meeting_id=? AND name=?", (mid, name))
        c.execute("UPDATE stages SET options='{\"expected_speakers\": 2}' WHERE meeting_id=? AND name='diarize'", (mid,))
        c.execute("UPDATE meetings SET status='queued', error=NULL WHERE id=?", (mid,))
    state["fail"] = False
    seen = {}
    rec.overrides["diarize"] = lambda ctx: seen.update(ctx.options)
    rec.calls.clear()
    w.run_once()
    assert rec.calls == STAGES[2:]
    assert seen == {"expected_speakers": 2}
    assert meeting(settings, mid)["status"] == "done"


def test_unexpected_exception(settings):
    mid = new_meeting(settings)

    def probe(ctx):
        raise KeyError("boom")

    worker.Worker(settings, runners=Recorder({"probe": probe}).runners()).run_once()
    rows = stage_rows(settings, mid)
    assert rows["probe"]["status"] == "failed" and "KeyError" in rows["probe"]["error"]
    assert meeting(settings, mid)["status"] == "failed"


def test_resume_after_crash(settings):
    mid = new_meeting(settings)
    with db.opened(settings.db_path) as c:
        c.execute("UPDATE stages SET status='done', progress=1 WHERE meeting_id=? AND name IN ('probe','extract_audio')", (mid,))
        c.execute("UPDATE stages SET status='running', progress=0.4 WHERE meeting_id=? AND name='diarize'", (mid,))
        c.execute("UPDATE meetings SET status='running' WHERE id=?", (mid,))
    rec = Recorder()
    worker.Worker(settings, runners=rec.runners()).run_once()
    assert rec.calls == STAGES[2:]
    assert meeting(settings, mid)["status"] == "done"


def test_cancellation_by_delete(settings):
    mid = new_meeting(settings)
    observed = {}

    def diarize(ctx):
        assert not ctx.should_stop()
        with ctx.db() as c:  # user deletes the meeting mid-stage
            c.execute("UPDATE meetings SET status='deleting' WHERE id=?", (ctx.meeting_id,))
        observed["stop"] = ctx.should_stop()

    rec = Recorder({"diarize": diarize})
    w = worker.Worker(settings, runners=rec.runners())
    w.run_once()
    assert observed["stop"] is True
    assert "transcribe" not in rec.calls
    assert meeting(settings, mid) is None  # purged after the stage stopped
    assert not settings.media_dir(mid).exists()
    assert sys.modules["quill.pipeline.stt"].cancelled == [mid]


def test_rerun_while_running_restarts(settings):
    mid = new_meeting(settings)
    runs = {"n": 0}

    def transcribe(ctx):
        runs["n"] += 1
        if runs["n"] == 1:
            with ctx.db() as c:  # API rerun from diarize while transcribe is running
                c.execute("UPDATE stages SET status='pending' WHERE meeting_id=? AND name IN "
                          "('diarize','transcribe','key_moments','frames','synthesize')", (ctx.meeting_id,))
                c.execute("UPDATE meetings SET status='queued' WHERE id=?", (ctx.meeting_id,))
            assert ctx.should_stop()

    rec = Recorder({"transcribe": transcribe})
    worker.Worker(settings, runners=rec.runners()).run_once()
    assert rec.calls == ["probe", "extract_audio", "diarize", "transcribe", "diarize", "transcribe",
                         "key_moments", "frames", "synthesize"]
    assert meeting(settings, mid)["status"] == "done"


def test_shutdown_leaves_stage_running(settings):
    mid = new_meeting(settings)
    holder = {}

    def diarize(ctx):
        holder["w"].stop()
        assert ctx.should_stop()

    w = worker.Worker(settings, runners=Recorder({"diarize": diarize}).runners())
    holder["w"] = w
    w.run_once()
    assert stage_rows(settings, mid)["diarize"]["status"] == "running"
    rec = Recorder()
    worker.Worker(settings, runners=rec.runners()).run_once()
    assert rec.calls == STAGES[2:]


def test_one_meeting_at_a_time_in_order(settings):
    first = new_meeting(settings, title="first")
    second = new_meeting(settings, title="second")
    order = []
    rec = Recorder({"synthesize": lambda ctx: order.append(ctx.meeting_id)})
    w = worker.Worker(settings, runners=rec.runners())
    w.run_once()
    assert order == [first] and meeting(settings, second)["status"] == "queued"
    w.run_once()
    assert order == [first, second]


def test_lazy_stage_mapping(settings, monkeypatch):
    mid = new_meeting(settings, mode="audio")
    calls = []
    fake = types.ModuleType("fake_stages")
    for fn in ("run_probe", "run_extract", "run"):
        setattr(fake, fn, (lambda n: (lambda ctx: calls.append((n, ctx.stage))))(fn))
    monkeypatch.setitem(sys.modules, "fake_stages", fake)
    monkeypatch.setattr(stages, "STAGE_MODULES", {name: ("fake_stages", attr) for name, (_, attr)
                                                  in stages.STAGE_MODULES.items()})
    worker.Worker(settings).run_once()
    assert calls == [("run_probe", "probe"), ("run_extract", "extract_audio"), ("run", "diarize"),
                     ("run", "transcribe"), ("run", "synthesize")]
    assert meeting(settings, mid)["status"] == "done"


def test_effective_settings_reach_stages(settings):
    new_meeting(settings)
    with db.opened(settings.db_path) as c:
        db.save_overrides(c, {"stt_model": "aer-stt-qwen3"})
    seen = {}
    rec = Recorder({"transcribe": lambda ctx: seen.setdefault("m", ctx.settings.stt_model)})
    worker.Worker(settings, runners=rec.runners()).run_once()
    assert seen["m"] == "aer-stt-qwen3"


def _finished_meeting(settings, mode, has_video, finished):
    mid = new_meeting(settings, mode=mode)
    src = settings.media_dir(mid) / "source.mp4"
    src.write_bytes(b"video")
    (settings.media_dir(mid) / "stt.opus").write_bytes(b"audio")
    with db.opened(settings.db_path) as c:
        c.execute("UPDATE meetings SET status='done', has_video=?, source_path=? WHERE id=?", (has_video, str(src), mid))
        c.execute("UPDATE stages SET status='done', finished_at=? WHERE meeting_id=?", (finished, mid))
    return mid, src


def test_retention_sweep(settings):
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(timespec="seconds")
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    video_old, src1 = _finished_meeting(settings, "video", 1, old)
    video_new, src2 = _finished_meeting(settings, "video", 1, recent)
    audio_old, src3 = _finished_meeting(settings, "audio", 0, old)
    assert worker.retention_sweep(settings) == [video_old]
    assert not src1.exists() and src2.exists() and src3.exists()
    assert (settings.media_dir(video_old) / "stt.opus").exists()
    assert meeting(settings, video_old)["source_deleted_at"]
    assert worker.retention_sweep(settings) == []  # idempotent
    with db.opened(settings.db_path) as c:
        db.save_overrides(c, {"video_retention_days": -1})
    later = datetime.now(timezone.utc) + timedelta(days=100)
    assert worker.retention_sweep(settings, now=later) == []
    with db.opened(settings.db_path) as c:
        db.save_overrides(c, {"video_retention_days": 0})
    assert worker.retention_sweep(settings) == [video_new]


def test_orphan_media_sweep(settings):
    keep = new_meeting(settings)
    orphan = settings.media_root / "deadbeef"
    orphan.mkdir()
    assert worker.sweep_orphan_media(settings, min_age_seconds=0) == ["deadbeef"]
    assert settings.media_dir(keep).exists()
