"""ffprobe/ffmpeg helpers and the probe / extract_audio stages (real ffmpeg,
tiny media generated per session)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fixtures.c1_helpers import (  # noqa: E402
    FakeCtx,
    make_audio_with_cover,
    make_beeps_opus,
    make_silent_video,
    make_video,
    needs_ffmpeg,
    silence_starts,
)

from quill.pipeline import media  # noqa: E402
from quill.stages import StageFailed  # noqa: E402

pytestmark = needs_ffmpeg


@pytest.fixture(scope="session")
def assets(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    return {
        "video": make_video(d / "video.mp4"),
        "cover": make_audio_with_cover(d / "song.mp3"),
        "beeps": make_beeps_opus(d / "beeps.opus"),
        "silent": make_silent_video(d / "silent.mp4"),
    }


# ---------------------------------------------------------------- probe


def test_probe_video(assets):
    s = media.probe(assets["video"])
    assert s["has_video"] and s["has_audio"]
    assert (s["width"], s["height"]) == (320, 240)
    assert s["video_codec"] == "h264" and s["audio_codec"] == "aac"
    assert s["duration"] == pytest.approx(9.0, abs=0.2)
    assert media.choose_mode(s) == "video"
    assert media.choose_mode(s, audio_only=True) == "audio"


def test_probe_cover_art_is_audio(assets):
    s = media.probe(assets["cover"])
    assert s["has_audio"] and not s["has_video"]
    assert media.choose_mode(s) == "audio"


def test_probe_garbage_fails(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_bytes(b"not media at all")
    with pytest.raises(StageFailed):
        media.probe(bad)


def _ctx_with_source(tmp_path, src: Path, mode="video") -> FakeCtx:
    ctx = FakeCtx(tmp_path, mode=mode)
    dest = ctx.media_dir / ("source" + src.suffix)
    os.link(src, dest)
    ctx.exec("UPDATE meetings SET source_path=? WHERE id='m1'", str(dest))
    return ctx


def test_run_probe_updates_meeting(tmp_path, assets):
    ctx = _ctx_with_source(tmp_path, assets["video"])
    media.run_probe(ctx)
    row = ctx.q("SELECT * FROM meetings WHERE id='m1'")[0]
    assert row["mode"] == "video" and row["has_video"] == 1
    assert (row["width"], row["height"]) == (320, 240)
    assert row["duration_s"] == pytest.approx(9.0, abs=0.2)
    assert ctx.mode == "video"
    assert (ctx.media_dir / "probe.json").exists()


def test_run_probe_honours_audio_only(tmp_path, assets):
    ctx = _ctx_with_source(tmp_path, assets["video"], mode="audio")
    media.run_probe(ctx)
    row = ctx.q("SELECT mode, has_video FROM meetings WHERE id='m1'")[0]
    assert row["mode"] == "audio" and row["has_video"] == 1


def test_run_probe_finds_source_by_glob(tmp_path, assets):
    ctx = FakeCtx(tmp_path)
    os.link(assets["cover"], ctx.media_dir / "source.mp3")
    media.run_probe(ctx)
    assert ctx.q("SELECT mode FROM meetings")[0]["mode"] == "audio"


def test_run_probe_without_audio_fails(tmp_path, assets):
    ctx = _ctx_with_source(tmp_path, assets["silent"])
    with pytest.raises(StageFailed, match="no audio"):
        media.run_probe(ctx)


# ---------------------------------------------------------------- extraction


def test_run_extract_outputs_and_progress(tmp_path, assets):
    ctx = _ctx_with_source(tmp_path, assets["video"])
    media.run_probe(ctx)
    media.run_extract(ctx)
    flac, opus = ctx.media_dir / "diar.flac", ctx.media_dir / "stt.opus"
    fi = media.ffprobe(flac)["streams"][0]
    oi = media.ffprobe(opus)["streams"][0]
    assert (fi["codec_name"], int(fi["sample_rate"]), fi["channels"]) == ("flac", 16000, 1)
    assert oi["codec_name"] == "opus" and oi["channels"] == 1
    assert media.probe_duration(opus) == pytest.approx(9.0, abs=0.1)
    assert media.probe_duration(flac) == pytest.approx(9.0, abs=0.1)
    fracs = [f for f, _ in ctx.progress_calls]
    assert fracs[-1] == 1.0
    assert not list(ctx.media_dir.glob("*.part"))

    # idempotent: second run reuses outputs without re-encoding
    before = (flac.stat().st_mtime_ns, opus.stat().st_mtime_ns)
    media.run_extract(ctx)
    assert (flac.stat().st_mtime_ns, opus.stat().st_mtime_ns) == before


def test_extract_redoes_truncated_output(tmp_path, assets):
    ctx = _ctx_with_source(tmp_path, assets["video"])
    media.run_probe(ctx)
    (ctx.media_dir / "diar.flac").write_bytes(b"junk")
    (ctx.media_dir / "stt.opus").write_bytes(b"junk")
    media.run_extract(ctx)
    assert media.probe_duration(ctx.media_dir / "stt.opus") == pytest.approx(9.0, abs=0.1)


def test_progress_parser():
    got = []
    h = media.progress_parser(10.0, got.append)
    for line in ["frame=1", "out_time_us=2500000", "out_time_ms=5000000", "out_time_us=N/A", "progress=end"]:
        h(line)
    assert got == [0.25, 0.5, 1.0]


def test_cancellation_kills_process(tmp_path):
    t0 = time.monotonic()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(media.Cancelled):
        media.run_process(["sleep", "30"], should_stop=stop)
    assert time.monotonic() - t0 < 5


def test_run_process_timeout():
    with pytest.raises(StageFailed, match="timed out"):
        media.run_process(["sleep", "30"], timeout=0.5)


# ---------------------------------------------------------------- clips


@pytest.mark.parametrize("start,end,beep_at", [(20.37, 21.61, 0.13), (39.2, 41.0, 0.8), (0.0, 2.2, None)])
def test_cut_clip_copy_is_accurate(tmp_path, assets, start, end, beep_at):
    out = media.cut_clip(assets["beeps"], start, end, tmp_path / "c.opus")
    assert media.probe_duration(out) == pytest.approx(end - start, abs=0.05)
    if beep_at is not None:
        sound = silence_starts(out)
        assert sound, "beep missing from clip"
        assert sound[0][0] == pytest.approx(beep_at, abs=0.05)


def test_cut_clip_is_deterministic(tmp_path, assets):
    from quill.pipeline.stt import sha256_file

    a = media.cut_clip(assets["beeps"], 10.0, 15.0, tmp_path / "a.opus")
    b = media.cut_clip(assets["beeps"], 10.0, 15.0, tmp_path / "b.opus")
    assert sha256_file(a) == sha256_file(b)


def test_cut_clip_falls_back_to_reencode(tmp_path, assets, monkeypatch):
    real = media._check

    def broken_copy(cmd, timeout=120.0):
        if "copy" in cmd:
            class R:
                returncode, stdout, stderr = 1, "", "boom"
            return R()
        return real(cmd, timeout)

    monkeypatch.setattr(media, "_check", broken_copy)
    out = media.cut_clip(assets["beeps"], 5.0, 8.0, tmp_path / "c.opus")
    assert media.probe_duration(out) == pytest.approx(3.0, abs=0.05)


def test_cut_clip_rejects_empty_span(tmp_path, assets):
    with pytest.raises(ValueError):
        media.cut_clip(assets["beeps"], 5.0, 5.0, tmp_path / "c.opus")


# ---------------------------------------------------------------- video helpers


def test_scene_changes(assets):
    cuts = media.scene_changes(assets["video"], 0.3)
    assert len(cuts) >= 2
    assert any(abs(t - 3.0) <= 0.5 for t in cuts)
    assert any(abs(t - 6.0) <= 0.5 for t in cuts)


def test_extract_frame_and_thumbnail(tmp_path, assets):
    from PIL import Image

    jpg, thumb = media.extract_frame(
        assets["video"], 1.0, tmp_path / "f.jpg", long_edge=200, thumb=tmp_path / "t.jpg", thumb_edge=64
    )
    with Image.open(jpg) as im:
        assert max(im.size) == 200
        r, g, b = im.convert("RGB").getpixel((50, 50))
        assert r > 200 and g < 60 and b < 60  # red third
    with Image.open(thumb) as im:
        assert max(im.size) == 64


def test_extract_frame_does_not_upscale(tmp_path, assets):
    from PIL import Image

    jpg, thumb = media.extract_frame(assets["video"], 4.0, tmp_path / "f.jpg")
    assert thumb is None
    with Image.open(jpg) as im:
        assert im.size == (320, 240)


def test_phash_dhash(tmp_path, assets):
    from PIL import Image, ImageDraw

    def slide(text_y: int, color="black") -> Image.Image:
        im = Image.new("RGB", (640, 360), "white")
        d = ImageDraw.Draw(im)
        d.rectangle([40, text_y, 600, text_y + 40], fill=color)
        d.rectangle([40, 250, 300, 330], fill="gray")
        return im

    a, a2, b = slide(40), slide(40).resize((1280, 720)), slide(150, "navy")
    ha, ha2, hb = media.phash(a), media.phash(a2), media.phash(b)
    assert len(ha) == 16
    assert media.hamming(ha, ha2) <= 4  # same slide at another resolution
    assert media.hamming(ha, hb) >= 10  # different slide
    assert media.hamming(media.dhash(a), media.dhash(a2)) <= 4
    # paths work too
    a.save(tmp_path / "a.png")
    assert media.phash(tmp_path / "a.png") == ha


def test_adaptive_cuts_finds_subtle_slide_flips():
    from quill.pipeline.media import adaptive_cuts
    # static slides: only the flips are reported, each ~0.06 (well below 0.3)
    scored = [(120.0, 0.059), (240.0, 0.058), (360.0, 0.058)]
    assert adaptive_cuts(scored, fps=2.0) == [120.0, 240.0, 360.0]


def test_adaptive_cuts_ignores_jitter_in_moving_footage():
    from quill.pipeline.media import adaptive_cuts
    # webcam-like motion: every frame scores 0.03-0.06, a real cut scores 0.35
    scored = [(i / 2, 0.03 + 0.03 * ((i * 7) % 5) / 4) for i in range(240)]
    scored[100] = (50.0, 0.35)
    assert adaptive_cuts(scored, fps=2.0) == [50.0]


def test_adaptive_cuts_ignores_tiny_changes():
    from quill.pipeline.media import adaptive_cuts
    assert adaptive_cuts([(10.0, 0.025), (70.0, 0.03)], fps=2.0) == []
