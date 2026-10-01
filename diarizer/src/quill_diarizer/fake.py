"""Deterministic fake model output for dev and tests (QUILL_DIARIZER_FAKE=1). No torch.

Produces a synthetic [T, 8] probability matrix from the input duration only, so
the same file always yields the same turns, and runs it through the real
post-processing. Pattern: speakers take turns of 4-15 s, with a short
overlap every few turns and occasional sub-0.3 s blips (which get dropped).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np

FAKE_MODEL = "fake/quill-diarizer"
FAKE_SPEAKERS = 3


def probe_duration(path: Path) -> float:
    try:
        import soundfile as sf

        return float(sf.info(str(path)).duration)
    except Exception:  # noqa: BLE001
        pass
    if shutil.which("ffprobe"):
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
        try:
            return float(proc.stdout.strip())
        except ValueError:
            pass
    return 60.0  # unreadable placeholder files (tests) get one fake minute


def fake_probs(duration_s: float, n_speakers: int = FAKE_SPEAKERS, frame_s: float = 0.01) -> np.ndarray:
    n_speakers = max(1, min(8, n_speakers))
    T = int(round(duration_s / frame_s))
    probs = np.full((T, 8), 0.02, dtype=np.float32)
    rng = np.random.default_rng(int(duration_s * 1000) % (2**32))
    t, turn, spk = 0.5, 0, 0
    while t < duration_s:
        length = float(rng.uniform(4.0, 15.0))
        s, e = int(t / frame_s), int(min(t + length, duration_s) / frame_s)
        probs[s:e, spk] = 0.9
        if turn % 4 == 3 and n_speakers > 1:   # the next speaker cuts in 0.6 s early
            nxt = (spk + 1) % n_speakers
            probs[max(s, e - 60):e, nxt] = 0.8
        if turn % 5 == 2:                        # a blip shorter than min_segment_s
            b = e + 20
            probs[b:b + 15, (spk + 2) % n_speakers] = 0.7
        t += length + float(rng.uniform(0.2, 1.5))
        spk = (spk + 1) % n_speakers
        turn += 1
    return probs
