"""Nemotron-3-Diarization inference on CPU via Hugging Face Transformers.

The model is run in *streaming* mode but with the checkpoint's *offline* chunk
sizes (chunk 340 + right context 40 encoder frames = 30.4 s, FIFO 40, speaker
cache update period 300). Each forward sees one 30.4 s window plus the
Arrival-Order Speaker Cache, so the result matches the library's offline
forward, while only one chunk of audio and features is ever held in memory.
Audio is read from disk chunk by chunk (soundfile seek), never loaded whole.

torch / transformers are imported lazily so the package (and fake mode) works
without them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

DEFAULT_MODEL = "nvidia/Nemotron-3-Diarization"
SAMPLE_RATE = 16000
FRAME_S = 0.01  # model output frame (10 ms)


class InputError(Exception):
    """Input missing or undecodable."""


class ModelError(Exception):
    """Model or runtime could not be loaded."""


@dataclass
class ModelResult:
    probs: np.ndarray  # [T, 8] float32 speaker activity probabilities, 10 ms frames
    duration_s: float
    model: str


# ---------------------------------------------------------------- audio input

@contextmanager
def open_audio(path: Path) -> Iterator["object"]:
    """Yield a soundfile.SoundFile that is 16 kHz mono.

    diar.flac from the pipeline already is; anything else is transcoded once
    with ffmpeg into a temporary FLAC (streamed, constant memory)."""
    import soundfile as sf

    if not path.is_file():
        raise InputError(f"input not found: {path}")
    try:
        info = sf.info(str(path))
        native = info.samplerate == SAMPLE_RATE and info.channels == 1
    except Exception:  # noqa: BLE001 - unknown container, let ffmpeg try
        native = False
    if native:
        with sf.SoundFile(str(path)) as fh:
            yield fh
        return
    if not shutil.which("ffmpeg"):
        raise InputError("input is not 16 kHz mono and ffmpeg is not available to convert it")
    with tempfile.TemporaryDirectory(prefix="quill-diar-") as tmp:
        out = Path(tmp) / "in.flac"
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
             "-ar", str(SAMPLE_RATE), "-c:a", "flac", str(out)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0 or not out.exists():
            raise InputError(f"ffmpeg could not decode input: {proc.stderr.strip()[-500:]}")
        with sf.SoundFile(str(out)) as fh:
            yield fh


def _read(fh, start: int, end: int) -> np.ndarray:
    """Samples [start, end) as float32; negative start is clamped (never happens past chunk 0)."""
    start = max(start, 0)
    fh.seek(start)
    return fh.read(max(end - start, 0), dtype="float32", always_2d=False)


# ---------------------------------------------------------------- model

def resolve_model(model: str | None) -> str:
    return model or os.environ.get("QUILL_DIARIZER_MODEL") or DEFAULT_MODEL


def load(model_ref: str, threads: int):
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    try:
        import torch
        from transformers import AutoModelForAudioFrameClassification, AutoProcessor
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ModelError(f"torch/transformers not installed: {exc}") from exc

    try:
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        hf_logging.disable_progress_bar()
    except Exception:  # noqa: BLE001
        pass
    torch.set_num_threads(max(1, threads))
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass  # already set in this process
    try:
        processor = AutoProcessor.from_pretrained(model_ref)
        model = AutoModelForAudioFrameClassification.from_pretrained(model_ref, dtype=torch.float32)
    except Exception as exc:  # noqa: BLE001
        raise ModelError(f"could not load model {model_ref!r}: {exc}") from exc
    model.eval()

    # Register the checkpoint's offline chunking as a streaming mode.
    cfg = model.config
    modes = {k: tuple(v) for k, v in processor.streaming_modes.items()}
    modes["offline"] = (cfg.chunk_length, cfg.chunk_right_context)
    processor.streaming_modes = modes
    processor.set_streaming_mode("offline")
    return torch, processor, model


def new_offline_cache(model):
    from transformers.models.nemotron3_diarization.modeling_nemotron3_diarization import (
        Nemotron3DiarizationSpeakerCache,
    )

    cfg = model.config
    return Nemotron3DiarizationSpeakerCache(
        cfg.streaming_config,
        fifo_length=cfg.fifo_length,
        speaker_cache_update_period=cfg.speaker_cache_update_period,
    )


def run(
    path: Path,
    threads: int,
    model_ref: str | None = None,
    progress: Callable[[float], None] | None = None,
) -> ModelResult:
    model_ref = resolve_model(model_ref)
    with open_audio(Path(path)) as fh:
        total = fh.frames
        duration = total / SAMPLE_RATE
        torch, processor, model = load(model_ref, threads)
        n_spk = model.config.head_config.num_speakers
        if total < processor.feature_extractor.win_length:
            return ModelResult(np.zeros((0, n_spk), np.float32), duration, model_ref)

        # Pre-allocate the output (10 ms frames); 8 h = 2.9 M x 8 x 4 B = 92 MB.
        n_frames_est = total // processor.feature_extractor.hop_length + 2
        probs = np.zeros((n_frames_est, n_spk), dtype=np.float32)
        filled = 0

        def forward(audio: np.ndarray, first: bool, last: bool, cache):
            inputs = processor(audio, sampling_rate=SAMPLE_RATE, is_streaming=True,
                               is_first_audio_chunk=first, is_last_audio_chunk=last)
            out = model(**inputs, speaker_cache=cache)
            return out.logits[0].sigmoid().numpy(), out.speaker_cache

        cache = new_offline_cache(model)
        first_n = processor.num_samples_first_audio_chunk
        per_chunk = processor.num_samples_per_audio_chunk
        with torch.inference_mode():
            if total <= first_n:
                p, _ = forward(_read(fh, 0, total), True, True, cache)
                probs[: len(p)] = p
                filled = len(p)
            else:
                p, cache = forward(_read(fh, 0, first_n), True, False, cache)
                probs[: len(p)] = p
                filled = len(p)
                mel_idx = processor.num_mel_frames_per_step
                start = processor.audio_chunk_start(mel_idx)
                while (end := start + per_chunk) <= total:
                    if progress:
                        progress(start / total)
                    p, cache = forward(_read(fh, start, end), False, False, cache)
                    probs[filled: filled + len(p)] = p
                    filled += len(p)
                    mel_idx += processor.num_mel_frames_per_step
                    start = processor.audio_chunk_start(mel_idx)
                p, _ = forward(_read(fh, start, total), False, True, cache)
                n = min(len(p), probs.shape[0] - filled)
                probs[filled: filled + n] = p[:n]
                filled += n
        if progress:
            progress(1.0)
        return ModelResult(probs[:filled], duration, model_ref)
