"""Real-model check (skipped unless QUILL_DIARIZER_MODEL points at a model and torch is installed).

Chunked streaming with offline chunk sizes must reproduce the library's
whole-file offline forward (up to the final chunk, where edge padding of the
spectrogram differs slightly)."""
import os

import numpy as np
import pytest

MODEL = os.environ.get("QUILL_DIARIZER_MODEL")
pytestmark = pytest.mark.skipif(not MODEL, reason="QUILL_DIARIZER_MODEL not set")


def test_streaming_matches_offline(tmp_path):
    torch = pytest.importorskip("torch")
    sf = pytest.importorskip("soundfile")
    from quill_diarizer import engine

    audio_path = os.environ.get("QUILL_DIARIZER_TEST_AUDIO")
    if audio_path:
        audio, sr = sf.read(audio_path, dtype="float32", frames=16000 * 90)
        assert sr == 16000
    else:
        rng = np.random.default_rng(0)
        audio = (0.05 * rng.standard_normal(16000 * 90)).astype(np.float32)
    path = tmp_path / "a.flac"
    sf.write(path, audio, 16000)

    res = engine.run(path, threads=4, model_ref=MODEL)
    _, processor, model = engine.load(MODEL, 4)
    with torch.inference_mode():
        ref = model(**processor(audio, sampling_rate=16000)).logits[0].sigmoid().numpy()

    assert res.probs.shape[1] == 8
    assert abs(len(res.probs) - len(ref)) <= 2
    last_chunk_start = (len(audio) // 16000 // 27) * 2720 - 2720  # well before the final chunk
    n = min(len(ref), len(res.probs), max(last_chunk_start, 0))
    assert np.abs(res.probs[:n] - ref[:n]).max() < 1e-3
