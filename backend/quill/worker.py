"""Pipeline worker: `python -m quill.worker`.

A single long-running process that polls SQLite for meetings with status
queued/running/paused and runs their stages in order, one meeting at a time.

State machine per stage: pending -> running -> done | paused | failed
(video-only stages become `skipped` in audio mode). A stage left `running` by a
crash or restart is simply re-run (stages are idempotent). StagePaused sets the
stage and meeting to `paused` and retries after 30 s. StageFailed or any other
exception marks the stage and meeting `failed`; the rerun endpoint resets them.

The worker also purges meetings marked `deleting`, sweeps source video past the
retention window and drops stale tus uploads.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import db
from .config import Settings
from .stages import (DONE, FAILED, PAUSED, PENDING, RUNNING, SKIPPED, STAGES, VIDEO_ONLY_STAGES,
                     StageContext, StageFailed, StagePaused, now_iso, resolve_stage)

log = logging.getLogger("quill.worker")

PAUSE_RETRY_SECONDS = 30.0
POLL_SECONDS = 2.0
SWEEP_SECONDS = 600.0
ACTIVE_STATUSES = ("queued", "running", "paused")

StageRunner = Callable[[StageContext], None]


# ------------------------------------------------------------ deletion / retention

def cancel_remote_jobs(settings: Settings, meeting_id: str) -> None:
    """Ask the STT module to cancel in-flight Avifors jobs, if it is available."""
    try:
        from .pipeline import stt  # type: ignore[attr-defined]
    except Exception:  # module not built yet or import error
        return
    cancel = getattr(stt, "cancel_jobs", None)
    if cancel is None:
        return
    try:
        cancel(settings, meeting_id)
    except Exception:
        log.warning("meeting=%s cancel_jobs failed: %s", meeting_id, traceback.format_exc(limit=1))


def purge_meeting(settings: Settings, meeting_id: str) -> None:
    """Cancel remote jobs, remove files and all rows for a meeting (idempotent)."""
    cancel_remote_jobs(settings, meeting_id)
    media_dir = settings.media_dir(meeting_id)
    if media_dir.exists() and media_dir.resolve().parent == settings.media_root.resolve():
        shutil.rmtree(media_dir, ignore_errors=True)
    with db.opened(settings.db_path) as conn:
        db.delete_meeting_rows(conn, meeting_id)
    log.info("meeting=%s purged", meeting_id)


def _pipeline_finished_at(conn: sqlite3.Connection, meeting_id: str) -> str | None:
    row = conn.execute("SELECT max(finished_at) FROM stages WHERE meeting_id=?", (meeting_id,)).fetchone()
    return row[0]


def retention_sweep(settings: Settings, now: datetime | None = None) -> list[str]:
    """Delete source video of finished meetings older than VIDEO_RETENTION_DAYS.
    Audio-only sources are kept. Returns affected meeting ids."""
    settings = db.effective_settings(settings)
    days = settings.video_retention_days
    if days < 0:
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    affected: list[str] = []
    with db.opened(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT id, source_path FROM meetings WHERE status='done' AND source_deleted_at IS NULL"
            " AND (has_video=1 OR (has_video IS NULL AND mode='video'))").fetchall()
        for row in rows:
            finished = _pipeline_finished_at(conn, row["id"])
            if not finished or finished > cutoff:
                continue
            media_dir = settings.media_dir(row["id"])
            if row["source_path"]:
                src = Path(row["source_path"])
                if src.resolve().parent == media_dir.resolve():
                    src.unlink(missing_ok=True)
            (media_dir / "proxy.mp4").unlink(missing_ok=True)
            conn.execute("UPDATE meetings SET source_deleted_at=? WHERE id=?", (now_iso(), row["id"]))
            db.add_event(conn, row["id"], "retention", "source video deleted by retention policy")
            affected.append(row["id"])
    return affected


def sweep_orphan_media(settings: Settings, min_age_seconds: float = 3600) -> list[str]:
    """Remove media/<id> directories whose meeting row no longer exists."""
    removed: list[str] = []
    if not settings.media_root.exists():
        return removed
    with db.opened(settings.db_path) as conn:
        known = {r["id"] for r in conn.execute("SELECT id FROM meetings")}
    for child in settings.media_root.iterdir():
        if not child.is_dir() or child.name in known:
            continue
        if time.time() - child.stat().st_mtime < min_age_seconds:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child.name)
    return removed


# ------------------------------------------------------------ worker

class Worker:
    def __init__(self, settings: Settings, runners: dict[str, StageRunner] | None = None,
                 pause_retry: float = PAUSE_RETRY_SECONDS, poll: float = POLL_SECONDS):
        self.settings = settings
        self.runners = runners
        self.pause_retry = pause_retry
        self.poll = poll
        self._retry_at: dict[str, float] = {}
        self._stop = threading.Event()
        self._last_sweep = 0.0

    # -- plumbing
    def _connect(self) -> sqlite3.Connection:
        return db.connect(self.settings.db_path)

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _runner(self, name: str) -> StageRunner:
        if self.runners is not None:
            return self.runners[name]
        return resolve_stage(name)

    def _heartbeat(self, meeting_id: str | None = None, stage: str | None = None) -> None:
        with db.opened(self.settings.db_path) as conn:
            conn.execute(
                "INSERT INTO worker_status(id, heartbeat_at, meeting_id, stage, pid) VALUES(1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at, meeting_id=excluded.meeting_id,"
                " stage=excluded.stage, pid=excluded.pid",
                (now_iso(), meeting_id, stage, os.getpid()))

    # -- scheduling
    def purge_deleting(self) -> None:
        with db.opened(self.settings.db_path) as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM meetings WHERE status IN ('deleting','cancelled')")]
        for mid in ids:
            purge_meeting(self.settings, mid)
            self._retry_at.pop(mid, None)

    def next_meeting(self) -> str | None:
        now = time.monotonic()
        with db.opened(self.settings.db_path) as conn:
            rows = conn.execute(
                "SELECT id, status FROM meetings WHERE status IN ('queued','running','paused')"
                " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, created_at, rowid"
            ).fetchall()
        for row in rows:
            if row["status"] == "paused" and self._retry_at.get(row["id"], 0.0) > now:
                continue
            return row["id"]
        return None

    def run_once(self) -> bool:
        """One scheduling step. Returns True if a meeting was processed."""
        self.purge_deleting()
        if time.monotonic() - self._last_sweep > SWEEP_SECONDS:
            self._last_sweep = time.monotonic()
            try:
                retention_sweep(self.settings)
                sweep_orphan_media(self.settings)
                from .upload import sweep_stale_uploads
                sweep_stale_uploads(self.settings)
            except Exception:
                log.exception("sweep failed")
        meeting_id = self.next_meeting()
        self._heartbeat(meeting_id)
        if meeting_id is None:
            return False
        self.process_meeting(meeting_id)
        self.purge_deleting()
        return True

    def run_forever(self) -> None:
        log.info("quill worker started (pid %s)", os.getpid())
        while not self.stopping:
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker loop error")
                worked = False
            if not worked:
                self._stop.wait(self.poll)
        log.info("quill worker stopped")

    # -- the state machine
    def _next_stage(self, conn: sqlite3.Connection, meeting: sqlite3.Row) -> str | None:
        rows = {r["name"]: r for r in conn.execute("SELECT * FROM stages WHERE meeting_id=?", (meeting["id"],))}
        audio = meeting["mode"] == "audio"
        for name in STAGES:
            row = rows.get(name)
            if row is None:
                conn.execute("INSERT INTO stages(meeting_id, name, status, progress) VALUES(?,?,'pending',0)",
                             (meeting["id"], name))
                status = PENDING
            else:
                status = row["status"]
            if audio and name in VIDEO_ONLY_STAGES:
                if status != SKIPPED:
                    conn.execute("UPDATE stages SET status='skipped', progress=0, detail=NULL, error=NULL,"
                                 " finished_at=? WHERE meeting_id=? AND name=?", (now_iso(), meeting["id"], name))
                continue
            if status == DONE:
                continue
            if status == SKIPPED and audio:
                continue
            return name  # pending, running (resume), paused (retry), failed (after rerun reset), skipped->video
        return None

    def process_meeting(self, meeting_id: str) -> str:
        """Run stages until the meeting is done, paused, failed or stopped.
        Returns the resulting meeting status (or 'stopped'/'deleted')."""
        while True:
            if self.stopping:
                return "stopped"
            with db.opened(self.settings.db_path) as conn:
                meeting = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
                if meeting is None or meeting["status"] not in ACTIVE_STATUSES:
                    return meeting["status"] if meeting else "deleted"
                name = self._next_stage(conn, meeting)
                if name is None:
                    conn.execute("UPDATE meetings SET status='done', error=NULL WHERE id=?", (meeting_id,))
                    db.add_event(conn, meeting_id, "done", "pipeline finished")
                    self._retry_at.pop(meeting_id, None)
                    return "done"
                stage_row = conn.execute("SELECT options FROM stages WHERE meeting_id=? AND name=?",
                                         (meeting_id, name)).fetchone()
                conn.execute("UPDATE stages SET status='running', progress=0, detail=NULL, error=NULL,"
                             " started_at=?, finished_at=NULL WHERE meeting_id=? AND name=?",
                             (now_iso(), meeting_id, name))
                conn.execute("UPDATE meetings SET status='running', error=NULL WHERE id=?", (meeting_id,))
                db.add_event(conn, meeting_id, "stage", f"{name} started")
                mode = meeting["mode"] or "video"
            try:
                options = json.loads(stage_row["options"]) if stage_row and stage_row["options"] else {}
            except ValueError:
                options = {}
            self._heartbeat(meeting_id, name)
            outcome = self._run_stage(meeting_id, name, mode, options)
            if outcome in ("paused", "failed", "stopped", "deleted"):
                return outcome

    def _stage_still_ours(self, conn: sqlite3.Connection, meeting_id: str, name: str) -> bool:
        meeting = conn.execute("SELECT status FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        if meeting is None or meeting["status"] in ("deleting", "cancelled"):
            return False
        row = conn.execute("SELECT status FROM stages WHERE meeting_id=? AND name=?", (meeting_id, name)).fetchone()
        return row is not None and row["status"] == RUNNING

    def _run_stage(self, meeting_id: str, name: str, mode: str, options: dict) -> str:
        settings = db.effective_settings(self.settings)
        media_dir = settings.media_dir(meeting_id)
        media_dir.mkdir(parents=True, exist_ok=True)
        ctx = StageContext(meeting_id=meeting_id, settings=settings, media_dir=media_dir, mode=mode,
                           db=lambda: db.connect(settings.db_path), stage=name, options=options,
                           stop_check=lambda: self.stopping)
        started = time.monotonic()
        error: tuple[str, str] | None = None   # (kind, message)
        try:
            self._runner(name)(ctx)
        except StagePaused as exc:
            error = ("paused", exc.reason or "paused")
        except StageFailed as exc:
            error = ("failed", exc.message or "failed")
        except Exception as exc:  # unexpected: record type and message only
            log.error("meeting=%s stage=%s unexpected error\n%s", meeting_id, name, traceback.format_exc())
            error = ("failed", f"Unexpected error: {type(exc).__name__}: {str(exc)[:300]}")
        elapsed = time.monotonic() - started

        with db.opened(self.settings.db_path) as conn:
            meeting = conn.execute("SELECT status FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if meeting is None:
                return "deleted"
            if meeting["status"] in ("deleting", "cancelled"):
                return "deleted"
            if not self._stage_still_ours(conn, meeting_id, name):
                # Reset by a rerun while running: loop again from the new state.
                db.add_event(conn, meeting_id, "stage", f"{name} interrupted by rerun")
                return "reset"
            if self.stopping:
                # Leave the stage 'running' so the next worker start re-runs it.
                return "stopped"
            if error is None:
                conn.execute("UPDATE stages SET status='done', progress=1, finished_at=?, error=NULL"
                             " WHERE meeting_id=? AND name=?", (now_iso(), meeting_id, name))
                db.add_event(conn, meeting_id, "stage", f"{name} done in {elapsed:.1f}s")
                log.info("meeting=%s stage=%s done in %.1fs", meeting_id, name, elapsed)
                return "done"
            kind, message = error
            if kind == "paused":
                conn.execute("UPDATE stages SET status='paused', detail=? WHERE meeting_id=? AND name=?",
                             (message[:500], meeting_id, name))
                conn.execute("UPDATE meetings SET status='paused' WHERE id=?", (meeting_id,))
                db.add_event(conn, meeting_id, "paused", f"{name}: {message}")
                self._retry_at[meeting_id] = time.monotonic() + self.pause_retry
                log.info("meeting=%s stage=%s paused: %s", meeting_id, name, message)
                return "paused"
            conn.execute("UPDATE stages SET status='failed', error=?, finished_at=? WHERE meeting_id=? AND name=?",
                         (message[:2000], now_iso(), meeting_id, name))
            conn.execute("UPDATE meetings SET status='failed', error=? WHERE id=?",
                         (f"{name}: {message}"[:2000], meeting_id))
            db.add_event(conn, meeting_id, "failed", f"{name}: {message}")
            log.warning("meeting=%s stage=%s failed: %s", meeting_id, name, message)
            return "failed"


def main() -> None:
    logging.basicConfig(level=os.environ.get("QUILL_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    settings.ensure_dirs()
    db.init(settings.db_path)
    worker = Worker(settings)

    def handle(signum, _frame):
        log.info("signal %s: finishing current step", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    worker.run_forever()


if __name__ == "__main__":
    main()
