"""Test helpers for the C1 pipeline tests (media, diarize, stt).

Tiny media files are generated with ffmpeg at test time (nothing binary is
committed), and FakeCtx implements the StageContext surface against a real
temp database from ``quill.db.init``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from quill import db as qdb

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_video(path: Path, seconds: int = 9) -> Path:
    """320x240 10 fps: red / blue / green thirds (2 hard cuts) + 440 Hz tone."""
    third = seconds / 3
    ff(
        "-f", "lavfi", "-i", f"color=c=red:s=320x240:r=10:d={third}",
        "-f", "lavfi", "-i", f"color=c=blue:s=320x240:r=10:d={third}",
        "-f", "lavfi", "-i", f"testsrc2=s=320x240:r=10:d={third}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a", "-ac", "2",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    )  # fmt: skip
    return path


def make_audio_with_cover(path: Path, seconds: int = 3) -> Path:
    """MP3 with an attached cover picture (must still count as audio)."""
    cover = path.with_suffix(".png")
    ff("-f", "lavfi", "-i", "color=c=yellow:s=64x64:d=1", "-frames:v", "1", str(cover))
    ff(
        "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}", "-i", str(cover),
        "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame", "-c:v", "png",
        "-disposition:v:0", "attached_pic", str(path),
    )  # fmt: skip
    return path


def make_beeps_opus(path: Path, seconds: int = 60, beeps=((20.5, 21.0), (40.0, 40.4))) -> Path:
    expr = "+".join(f"between(t,{a},{b})" for a, b in beeps)
    ff(
        "-f", "lavfi", "-i", f"aevalsrc='if({expr},sin(2*PI*440*t),0)':s=16000:d={seconds}",
        "-c:a", "libopus", "-b:a", "32k", str(path),
    )  # fmt: skip
    return path


def make_silent_video(path: Path, seconds: int = 2) -> Path:
    ff("-f", "lavfi", "-i", f"color=c=red:s=160x120:r=10:d={seconds}", "-c:v", "libx264",
       "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path))
    return path


def silence_starts(path: Path) -> list[tuple[float, float]]:
    """(silence_end, next_silence_start) pairs = where sound is."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=n=-30dB:d=0.05", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr  # fmt: skip
    import re

    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", out)]
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", out)]
    return list(zip(ends, starts[1:]))


class FakeSettings:
    def __init__(self, **kw: Any) -> None:
        self.data_dir = kw.pop("data_dir", Path("/tmp/quill-test"))
        self.gateway_url = kw.pop("gateway_url", "http://gw.test/v1")
        self.stt_model = kw.pop("stt_model", "aer-stt-v1")
        self.stt_concurrency = kw.pop("stt_concurrency", 3)
        self.diarizer_cmd = kw.pop("diarizer_cmd", "quill-diarize")
        for k, v in kw.items():
            setattr(self, k, v)


class FakeCtx:
    def __init__(self, tmp: Path, *, settings: Any = None, mode: str = "video", **meeting: Any) -> None:
        self.db_path = tmp / "db" / "quill.sqlite3"
        qdb.init(self.db_path)
        self.media_dir = tmp / "media" / "m1"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.meeting_id = "m1"
        self.mode = mode
        self.settings = settings or FakeSettings(data_dir=tmp)
        self.options: dict[str, Any] = {}
        self.progress_calls: list[tuple[float, str]] = []
        self.logs: list[str] = []
        self.stop = False
        conn = self.db()
        with conn:
            qdb.create_meeting(conn, owner_id=None, title="t", meeting_id="m1", mode=mode, **meeting)
        conn.close()

    def db(self):
        return qdb.connect(self.db_path)

    def progress(self, fraction: float, detail: str = "") -> None:
        self.progress_calls.append((fraction, detail))

    def should_stop(self) -> bool:
        return self.stop

    def log(self, message: str) -> None:
        self.logs.append(message)

    def q(self, sql: str, *args: Any) -> list:
        conn = self.db()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def exec(self, sql: str, *args: Any) -> None:
        conn = self.db()
        try:
            with conn:
                conn.execute(sql, args)
        finally:
            conn.close()
