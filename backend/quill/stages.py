"""Stage contract shared by the worker and the pipeline modules.

Each stage module exposes `run(ctx: StageContext) -> None`. Stages are idempotent:
they clean/replace their own prior output rows before writing and may reuse
finished sub-work. Raise StagePaused to wait (retried after 30 s) and StageFailed
for a user-visible failure; anything else is reported as an unexpected failure.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Settings

log = logging.getLogger("quill.stages")

STAGES = ["probe", "extract_audio", "diarize", "transcribe",
          "key_moments", "frames", "synthesize"]
VIDEO_ONLY_STAGES = frozenset({"key_moments", "frames"})

# name -> (module, function); imported lazily so the API never pulls pipeline deps.
STAGE_MODULES: dict[str, tuple[str, str]] = {
    "probe": ("quill.pipeline.media", "run_probe"),
    "extract_audio": ("quill.pipeline.media", "run_extract"),
    "diarize": ("quill.pipeline.diarize", "run"),
    "transcribe": ("quill.pipeline.stt", "run"),
    "key_moments": ("quill.pipeline.moments", "run"),
    "frames": ("quill.pipeline.vision", "run"),
    "synthesize": ("quill.pipeline.synthesis", "run"),
}

# Stage status values. "skipped" is used for video-only stages in audio mode.
PENDING, RUNNING, PAUSED, FAILED, DONE, SKIPPED = (
    "pending", "running", "paused", "failed", "done", "skipped")


class StagePaused(Exception):
    """Temporary condition (e.g. STT model disabled). Worker retries after 30 s."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class StageFailed(Exception):
    """Permanent failure for this run; the user can retry the stage."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stages_for_mode(mode: str | None) -> list[str]:
    if mode == "audio":
        return [s for s in STAGES if s not in VIDEO_ONLY_STAGES]
    return list(STAGES)


def resolve_stage(name: str) -> Callable[["StageContext"], None]:
    module_name, attr = STAGE_MODULES[name]
    module = importlib.import_module(module_name)
    return getattr(module, attr)


@dataclass
class StageContext:
    meeting_id: str
    settings: Settings
    media_dir: Path
    mode: str                                   # "video" | "audio"
    db: Callable[[], sqlite3.Connection]        # opens a new connection (row_factory=Row)
    stage: str = ""                             # addition: current stage name
    options: dict[str, Any] = field(default_factory=dict)  # addition: rerun options
    stop_check: Callable[[], bool] | None = None  # addition: extra stop signal (worker shutdown)

    def _conn(self) -> sqlite3.Connection:
        return self.db()

    def progress(self, fraction: float, detail: str = "") -> None:
        try:
            fraction = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            fraction = 0.0
        if not self.stage:
            return
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE stages SET progress=?, detail=? WHERE meeting_id=? AND name=? AND status='running'",
                    (fraction, detail[:500], self.meeting_id, self.stage))
        finally:
            conn.close()

    def should_stop(self) -> bool:
        """True when the meeting was deleted/cancelled, the stage was reset by a
        rerun, or the worker is shutting down."""
        if self.stop_check is not None and self.stop_check():
            return True
        conn = self._conn()
        try:
            row = conn.execute("SELECT status FROM meetings WHERE id=?", (self.meeting_id,)).fetchone()
            if row is None or row["status"] in ("cancelled", "deleting"):
                return True
            if self.stage:
                st = conn.execute("SELECT status FROM stages WHERE meeting_id=? AND name=?",
                                  (self.meeting_id, self.stage)).fetchone()
                if st is None or st["status"] != RUNNING:
                    return True
            return False
        finally:
            conn.close()

    def log(self, message: str) -> None:
        """Operational log line (IDs, timings, counts). Never transcript text."""
        log.info("meeting=%s stage=%s %s", self.meeting_id, self.stage, message)
        conn = self._conn()
        try:
            with conn:
                conn.execute("INSERT INTO events(meeting_id, at, kind, message) VALUES(?,?,?,?)",
                             (self.meeting_id, now_iso(), "log", f"[{self.stage}] {message}"[:2000]))
        finally:
            conn.close()
