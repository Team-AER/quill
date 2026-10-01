"""ffmpeg / ffprobe helpers and the ``probe`` + ``extract_audio`` stages (PLAN §4.2).

Stages
------
- ``run_probe(ctx)``: ffprobe the source, choose the mode, update ``meetings``.
- ``run_extract(ctx)``: ONE decode pass -> ``diar.flac`` (16 kHz mono s16 FLAC for
  the diarizer) and ``stt.opus`` (16 kHz mono 32 kbps libopus for STT), with
  progress from ``-progress pipe:1``. Idempotent: outputs are written to temp
  names and renamed; an existing complete pair is reused.

Helpers used by other stages: ``cut_clip``, ``scene_changes``, ``extract_frame``,
``make_thumbnail``, ``phash`` / ``dhash`` / ``hamming``.

Opus clip cutting (verified with ffmpeg 9.0.2): an input-side ``-ss`` with
``-c copy`` keeps the whole preceding Ogg page as negative-timestamp pre-roll
that decoders still emit (clip up to ~1 s too long, shifted). We therefore seek
coarsely on the input (3 s early) and trim precisely on the output side, still
stream-copying. Measured error <= 20 ms in duration and alignment. If the copy
result is off by more than ``CUT_TOLERANCE_S`` (or ffmpeg fails) we re-encode.
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from quill.stages import StageFailed

FFMPEG = os.environ.get("QUILL_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("QUILL_FFPROBE", "ffprobe")
SAMPLE_RATE = 16000
OPUS_BITRATE = "32k"
CUT_TOLERANCE_S = 0.12
COARSE_SEEK_S = 3.0
# Deterministic Ogg/FLAC output (fixed stream serial, no encoder string) so
# re-cutting the same span yields the same bytes and the same sha256 / key.
BITEXACT = ["-fflags", "+bitexact", "-flags:a", "+bitexact"]


class Cancelled(StageFailed):
    """Raised when ctx.should_stop() became true while a subprocess ran."""

    def __init__(self, message: str = "cancelled") -> None:
        super().__init__(message)


# ---------------------------------------------------------------- subprocesses


def run_process(
    cmd: list[str],
    *,
    should_stop: Callable[[], bool] | None = None,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
    tail: int = 40,
    timeout: float | None = None,
) -> tuple[int, list[str], list[str]]:
    """Run a subprocess, streaming lines to callbacks, killing its process group
    when ``should_stop()`` turns true. Returns (returncode, stdout_tail, stderr_tail).
    Raises Cancelled when stopped."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        bufsize=1,
        env=env,
        start_new_session=True,
    )
    tails = (collections.deque(maxlen=tail), collections.deque(maxlen=tail))

    def pump(stream, sink, cb):
        for line in stream:
            line = line.rstrip("\r\n")
            sink.append(line)
            if cb is not None:
                try:
                    cb(line)
                except Exception:  # a progress callback must never wedge the pipe
                    pass
        stream.close()

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, tails[0], on_stdout), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, tails[1], on_stderr), daemon=True),
    ]
    for t in threads:
        t.start()
    started = time.monotonic()
    stopped = False
    while True:
        try:
            proc.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pass
        if (should_stop is not None and should_stop()) or (
            timeout is not None and time.monotonic() - started > timeout
        ):
            stopped = should_stop is not None and should_stop()
            _kill(proc)
            if not stopped:
                for t in threads:
                    t.join(timeout=2)
                raise StageFailed(f"{Path(cmd[0]).name} timed out")
            break
    for t in threads:
        t.join(timeout=5)
    if stopped:
        raise Cancelled()
    return proc.returncode, list(tails[0]), list(tails[1])


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
    except ProcessLookupError:
        pass


def _check(cmd: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=timeout
    )


def _err_tail(lines: Iterable[str], limit: int = 400) -> str:
    text = " | ".join(l.strip() for l in lines if l.strip())
    return text[-limit:] if text else "unknown error"


# ---------------------------------------------------------------- probe


def ffprobe(path: str | os.PathLike) -> dict[str, Any]:
    res = _check(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=120,
    )
    if res.returncode != 0:
        raise StageFailed(f"ffprobe could not read the file: {_err_tail(res.stderr.splitlines())}")
    try:
        return json.loads(res.stdout or "{}")
    except ValueError as exc:
        raise StageFailed("ffprobe returned invalid JSON") from exc


def _float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _is_real_video(stream: dict[str, Any]) -> bool:
    if stream.get("codec_type") != "video":
        return False
    disp = stream.get("disposition") or {}
    if disp.get("attached_pic") or disp.get("timed_thumbnails") or disp.get("still_image"):
        return False  # cover art / thumbnails in audio files
    if str(stream.get("codec_name", "")).lower() in {"png", "bmp", "gif"}:
        return False
    frames = stream.get("nb_frames")
    if frames is not None and str(frames).isdigit() and int(frames) <= 1:
        return False
    return True


def summarize_probe(info: dict[str, Any]) -> dict[str, Any]:
    """Reduce ffprobe JSON to what Quill needs."""
    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if _is_real_video(s)]
    durations = [_float(fmt.get("duration"))] + [_float(s.get("duration")) for s in audio + video]
    durations = [d for d in durations if d and d > 0]
    duration = _float(fmt.get("duration")) or (max(durations) if durations else None)
    v = video[0] if video else None
    width = height = None
    if v is not None:
        width, height = v.get("width"), v.get("height")
        rot = None
        for side in v.get("side_data_list") or []:
            if "rotation" in side:
                rot = _float(side.get("rotation"))
        rot = rot if rot is not None else _float((v.get("tags") or {}).get("rotate"))
        if rot is not None and int(abs(rot)) % 180 == 90 and width and height:
            width, height = height, width
    return {
        "duration": duration,
        "has_audio": bool(audio),
        "has_video": v is not None,
        "width": int(width) if width else None,
        "height": int(height) if height else None,
        "audio_codec": audio[0].get("codec_name") if audio else None,
        "video_codec": v.get("codec_name") if v is not None else None,
        "container": fmt.get("format_name"),
        "audio_channels": audio[0].get("channels") if audio else None,
        "audio_sample_rate": _float(audio[0].get("sample_rate")) if audio else None,
    }


def probe(path: str | os.PathLike) -> dict[str, Any]:
    return summarize_probe(ffprobe(path))


def choose_mode(summary: dict[str, Any], audio_only: bool = False) -> str:
    return "video" if summary.get("has_video") and not audio_only else "audio"


def source_path(ctx: Any, row: Any | None = None) -> Path:
    """Resolve the meeting's source file (absolute, relative to media_dir, or
    media_dir/source.*)."""
    media_dir = Path(ctx.media_dir)
    raw = None
    if row is not None:
        try:
            raw = row["source_path"]
        except (KeyError, IndexError):
            raw = None
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = media_dir / p
        if p.exists():
            return p
    candidates = sorted(media_dir.glob("source.*"))
    if candidates:
        return candidates[0]
    raise StageFailed("source file is missing")


def _meeting(ctx: Any) -> Any:
    conn = ctx.db()
    try:
        row = conn.execute("SELECT * FROM meetings WHERE id=?", (ctx.meeting_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise StageFailed("meeting not found")
    return row


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def run_probe(ctx: Any) -> None:
    """Stage ``probe``. ``meetings.mode == 'audio'`` set at upload time
    (``audio_only`` metadata) is honoured; otherwise mode = video iff a real
    video stream exists."""
    row = _meeting(ctx)
    src = source_path(ctx, row)
    ctx.progress(0.1, "probing")
    summary = probe(src)
    if not summary["has_audio"]:
        raise StageFailed("the recording has no audio stream")
    if not summary["duration"]:
        raise StageFailed("could not determine the recording duration")
    audio_only = str(_row_get(row, "mode", "")).lower() == "audio"
    mode = choose_mode(summary, audio_only)
    try:
        (Path(ctx.media_dir) / "probe.json").write_text(json.dumps(summary, indent=1))
    except OSError:
        pass
    conn = ctx.db()
    try:
        with conn:
            conn.execute(
                "UPDATE meetings SET duration_s=?, has_video=?, width=?, height=?, mode=? WHERE id=?",
                (
                    summary["duration"],
                    1 if summary["has_video"] else 0,
                    summary["width"],
                    summary["height"],
                    mode,
                    ctx.meeting_id,
                ),
            )
    finally:
        conn.close()
    try:
        ctx.mode = mode
    except Exception:
        pass
    ctx.log(
        f"probe: {summary['duration']:.1f}s mode={mode} audio={summary['audio_codec']} "
        f"video={summary['video_codec']} {summary['width']}x{summary['height']}"
    )
    ctx.progress(1.0, mode)


# ---------------------------------------------------------------- extraction

_TIME_RE = re.compile(r"^out_time_(?:us|ms)=(\d+)$")


def progress_parser(duration: float | None, report: Callable[[float], None]) -> Callable[[str], None]:
    """Line handler for ``-progress pipe:1`` output (out_time_us is microseconds;
    so is out_time_ms, a historical ffmpeg misnomer)."""
    state = {"last": -1.0}

    def handle(line: str) -> None:
        m = _TIME_RE.match(line.strip())
        if m and duration:
            frac = min(1.0, max(0.0, int(m.group(1)) / 1e6 / duration))
            if frac - state["last"] >= 0.005:
                state["last"] = frac
                report(frac)
        elif line.strip() == "progress=end":
            report(1.0)

    return handle


def extract_cmd(src: Path, flac: Path, opus: Path) -> list[str]:
    return [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-filter_complex",
        f"[0:a:0]aresample={SAMPLE_RATE},aformat=sample_fmts=s16:channel_layouts=mono,asplit=2[d][s]",
        "-map", "[d]", "-c:a", "flac", *BITEXACT, "-f", "flac", str(flac),
        "-map", "[s]", "-c:a", "libopus", "-b:a", OPUS_BITRATE, "-application", "voip",
        *BITEXACT, "-f", "ogg", str(opus),
        "-progress", "pipe:1", "-nostats",
    ]  # fmt: skip


def _media_ok(path: Path, expect: float | None) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    if not expect:
        return True
    try:
        d = probe_duration(path)
    except StageFailed:
        return False
    return d is not None and abs(d - expect) <= max(1.0, expect * 0.01)


def probe_duration(path: str | os.PathLike) -> float | None:
    res = _check(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)], timeout=60
    )
    if res.returncode != 0:
        raise StageFailed(f"ffprobe failed: {_err_tail(res.stderr.splitlines())}")
    try:
        return _float(json.loads(res.stdout)["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return None


def extract_audio(
    src: Path,
    out_dir: Path,
    duration: float | None = None,
    *,
    progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    flac, opus = out_dir / "diar.flac", out_dir / "stt.opus"
    if _media_ok(flac, duration) and _media_ok(opus, duration):
        if progress:
            progress(1.0)
        return flac, opus
    tmp_flac, tmp_opus = out_dir / "diar.flac.part", out_dir / "stt.opus.part"
    for p in (tmp_flac, tmp_opus):
        p.unlink(missing_ok=True)
    handler = progress_parser(duration, progress) if progress else None
    try:
        code, _, err = run_process(
            extract_cmd(src, tmp_flac, tmp_opus), should_stop=should_stop, on_stdout=handler
        )
    except BaseException:
        for p in (tmp_flac, tmp_opus):
            p.unlink(missing_ok=True)
        raise
    if code != 0 or not tmp_flac.exists() or not tmp_opus.exists():
        for p in (tmp_flac, tmp_opus):
            p.unlink(missing_ok=True)
        raise StageFailed(f"audio extraction failed: {_err_tail(err)}")
    os.replace(tmp_flac, flac)
    os.replace(tmp_opus, opus)
    if progress:
        progress(1.0)
    return flac, opus


def run_extract(ctx: Any) -> None:
    """Stage ``extract_audio``."""
    row = _meeting(ctx)
    src = source_path(ctx, row)
    duration = _float(_row_get(row, "duration_s")) or probe(src)["duration"]
    extract_audio(
        src,
        Path(ctx.media_dir),
        duration,
        progress=lambda f: ctx.progress(f, "extracting audio"),
        should_stop=ctx.should_stop,
    )
    ctx.log("extract_audio: diar.flac + stt.opus ready")


# ---------------------------------------------------------------- clips


def cut_clip(
    src_opus: str | os.PathLike,
    start: float,
    end: float,
    out: str | os.PathLike,
    *,
    tolerance: float = CUT_TOLERANCE_S,
    verify: bool = True,
) -> Path:
    """Cut [start, end) from an Opus file into ``out`` (Ogg/Opus). Stream copy
    with coarse input seek + exact output trim; re-encode fallback."""
    src, out = Path(src_opus), Path(out)
    start = max(0.0, float(start))
    length = float(end) - start
    if length <= 0:
        raise ValueError("clip end must be after start")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    coarse = max(0.0, start - COARSE_SEEK_S)
    copy_cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{coarse:.3f}", "-i", str(src),
        "-ss", f"{start - coarse:.3f}", "-t", f"{length:.3f}",
        "-map", "0:a:0", "-c", "copy", "-avoid_negative_ts", "make_zero",
        *BITEXACT, "-f", "ogg", str(tmp),
    ]  # fmt: skip
    res = _check(copy_cmd)
    ok = res.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0
    if ok and verify:
        d = probe_duration(tmp)
        ok = d is not None and abs(d - length) <= tolerance + 0.02
    if not ok:
        enc_cmd = [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
            "-map", "0:a:0", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-c:a", "libopus", "-b:a", OPUS_BITRATE, "-application", "voip",
            *BITEXACT, "-f", "ogg", str(tmp),
        ]  # fmt: skip
        res = _check(enc_cmd)
        if res.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise StageFailed(f"clip cut failed: {_err_tail(res.stderr.splitlines())}")
    os.replace(tmp, out)
    return out


# ---------------------------------------------------------------- video helpers

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def scene_changes(
    source: str | os.PathLike,
    threshold: float = 0.3,
    *,
    fps: float = 2.0,
    height: int = 480,
    should_stop: Callable[[], bool] | None = None,
    progress: Callable[[float], None] | None = None,
    duration: float | None = None,
) -> list[float]:
    """Timestamps (s) of scene changes on a ``fps``, ``height``p decode. Cheap on CPU.

    A frame counts when its scene score exceeds ``threshold``, or when it is a
    clear spike over the local background (see ``adaptive_cuts``): a slide flip
    between two slides of one template scores only ~0.06, while webcam motion
    keeps the background high enough that its jitter is not mistaken for cuts."""
    vf = (
        f"fps={fps},scale=-2:'min({height},ih)':flags=fast_bilinear,"
        f"select='gt(scene\\,{SCENE_FLOOR})',metadata=print:file=-"
    )
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", vf, "-f", "null", "-",
    ]  # fmt: skip
    if progress:  # progress on stderr; stdout carries the metadata lines
        cmd += ["-progress", "pipe:2", "-nostats"]
    scored: list[tuple[float, float]] = []
    pending: list[float] = []

    def on_out(line: str) -> None:
        m = _PTS_RE.search(line)
        if m:
            pending[:] = [round(float(m.group(1)), 3)]
            return
        s = _SCORE_RE.search(line)
        if s and pending:
            scored.append((pending[0], float(s.group(1))))
            pending.clear()

    on_err = progress_parser(duration, progress) if progress else None
    code, _, err = run_process(cmd, should_stop=should_stop, on_stdout=on_out, on_stderr=on_err)
    if code != 0:
        raise StageFailed(f"scene detection failed: {_err_tail(err)}")
    return adaptive_cuts(scored, fps=fps, threshold=threshold)


SCENE_FLOOR = 0.02        # frames below this never reach Python (static frames score ~0)
SCENE_MIN_SPIKE = 0.04    # an adaptive cut still needs at least this score
SCENE_SPIKE_RATIO = 4.0   # ... and this multiple of the local median
SCENE_WINDOW_S = 30.0     # half-width of the local background window
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def adaptive_cuts(scored: Sequence[tuple[float, float]], *, fps: float,
                  threshold: float = 0.3, window_s: float = SCENE_WINDOW_S,
                  min_spike: float = SCENE_MIN_SPIKE, ratio: float = SCENE_SPIKE_RATIO) -> list[float]:
    """Pick cut times from (time, score) pairs of frames above SCENE_FLOOR (pure).

    Frames that were not reported scored ~0, so the local median over the
    ±window counts them as zeros. A frame is a cut if its score exceeds
    ``threshold`` or is at least ``min_spike`` and ``ratio`` x that median."""
    pts = sorted(scored)
    frames_in_window = max(1, int(round(2 * window_s * fps)) + 1)
    cuts: list[float] = []
    lo = 0
    for i, (t, score) in enumerate(pts):
        if score > threshold:
            cuts.append(t)
            continue
        if score < min_spike:
            continue
        while pts[lo][0] < t - window_s:
            lo += 1
        hi = i
        while hi + 1 < len(pts) and pts[hi + 1][0] <= t + window_s:
            hi += 1
        window = sorted(s for j, (_, s) in enumerate(pts[lo:hi + 1], lo) if j != i)
        zeros = max(0, frames_in_window - 1 - len(window))
        values = [0.0] * zeros + window
        median = values[len(values) // 2] if values else 0.0
        if score >= ratio * median:
            cuts.append(t)
    return sorted(set(cuts))


_SCALE_LONG_EDGE = (
    "scale='if(gte(iw,ih),min({e},iw),-2)':'if(gte(iw,ih),-2,min({e},ih))'"
)


def extract_frame(
    source: str | os.PathLike,
    t: float,
    out_jpg: str | os.PathLike,
    long_edge: int = 1280,
    *,
    thumb: str | os.PathLike | None = None,
    thumb_edge: int = 320,
    quality: int = 3,
) -> tuple[Path, Path | None]:
    """Grab the frame at ``t`` as JPEG (long edge <= ``long_edge``), optional
    thumbnail. Returns (jpg, thumb_or_None)."""
    out = Path(out_jpg)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.jpg")
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, float(t)):.3f}", "-i", str(source),
        "-map", "0:v:0", "-frames:v", "1",
        "-vf", _SCALE_LONG_EDGE.format(e=int(long_edge)),
        "-q:v", str(quality), str(tmp),
    ]  # fmt: skip
    res = _check(cmd)
    if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise StageFailed(f"frame extraction failed at {t:.1f}s: {_err_tail(res.stderr.splitlines())}")
    os.replace(tmp, out)
    thumb_path = make_thumbnail(out, thumb, thumb_edge) if thumb is not None else None
    return out, thumb_path


def make_thumbnail(src: str | os.PathLike, out: str | os.PathLike, long_edge: int = 320) -> Path:
    from PIL import Image

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
        tmp = out.with_name(out.stem + ".part.jpg")
        im.save(tmp, "JPEG", quality=82, optimize=True)
    os.replace(tmp, out)
    return out


# ---------------------------------------------------------------- perceptual hashes
# Pure Python + Pillow (numpy is not a dependency). A 32x32 DCT is ~65k
# multiply-adds, negligible next to decoding the frame.


def _gray(image: Any, size: tuple[int, int]) -> list[list[float]]:
    from PIL import Image

    if isinstance(image, (str, os.PathLike)):
        with Image.open(image) as im:
            g = im.convert("L").resize(size, Image.Resampling.LANCZOS)
    else:
        g = image.convert("L").resize(size, Image.Resampling.LANCZOS)
    w, h = size
    data = g.tobytes()  # mode "L": one byte per pixel
    return [[float(data[y * w + x]) for x in range(w)] for y in range(h)]


def _bits_to_hex(bits: list[bool]) -> str:
    value = 0
    for b in bits:
        value = (value << 1) | int(bool(b))
    return f"{value:0{(len(bits) + 3) // 4}x}"


def _dct_rows(n: int, keep: int) -> list[list[float]]:
    """First ``keep`` rows of the orthonormal DCT-II matrix of size n."""
    import math

    rows = []
    for k in range(keep):
        scale = math.sqrt((1.0 if k == 0 else 2.0) / n)
        rows.append([scale * math.cos(math.pi * (2 * i + 1) * k / (2 * n)) for i in range(n)])
    return rows


def phash(image: Any, hash_size: int = 8, highfreq_factor: int = 4) -> str:
    """64-bit DCT perceptual hash (hex, 16 chars). ``image`` is a path or PIL image.
    Low-frequency 8x8 DCT block of a 32x32 grayscale, thresholded at the median
    (DC excluded from the median)."""
    n = hash_size * highfreq_factor
    px = _gray(image, (n, n))
    d = _dct_rows(n, hash_size)
    # tmp = D_k @ px  (hash_size x n), then low = tmp @ D_k^T (hash_size x hash_size)
    tmp = [[sum(dk[i] * px[i][j] for i in range(n)) for j in range(n)] for dk in d]
    low = [[sum(tmp[r][j] * dk[j] for j in range(n)) for dk in d] for r in range(hash_size)]
    flat = [v for row in low for v in row]
    rest = sorted(flat[1:])
    mid = len(rest) // 2
    med = rest[mid] if len(rest) % 2 else (rest[mid - 1] + rest[mid]) / 2
    return _bits_to_hex([v > med for v in flat])


def dhash(image: Any, hash_size: int = 8) -> str:
    """64-bit horizontal difference hash (hex)."""
    px = _gray(image, (hash_size + 1, hash_size))
    return _bits_to_hex([row[x + 1] > row[x] for row in px for x in range(hash_size)])


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def have_ffmpeg() -> bool:
    return shutil.which(FFMPEG) is not None and shutil.which(FFPROBE) is not None
