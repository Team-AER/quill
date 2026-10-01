import dataclasses
from pathlib import Path

import pytest

from quill.config import GiB, Settings


def test_defaults():
    s = Settings.from_env({})
    assert s.data_dir == Path("/var/lib/quill")
    assert s.gateway_url == "http://localhost:4000/v1"
    assert s.stt_model == "aer-stt-v1"
    assert s.text_model == s.vision_model == "google/gemma-4-12B-it-qat-w4a16-ct"
    assert s.max_upload_bytes == 10 * GiB
    assert s.video_retention_days == 14
    assert (s.stt_concurrency, s.vision_concurrency, s.frames_per_hour, s.max_frames) == (3, 2, 40, 200)
    assert s.db_path == Path("/var/lib/quill/db/quill.sqlite3")
    assert s.media_dir("abc") == Path("/var/lib/quill/media/abc")
    assert not s.cookie_secure


def test_env_overrides_and_frozen(tmp_path):
    s = Settings.from_env({"QUILL_DATA_DIR": str(tmp_path), "QUILL_MAX_UPLOAD_BYTES": "1234",
                           "QUILL_PUBLIC_URL": "https://quill.example.com/", "QUILL_STT_MODEL": "aer-stt-qwen3",
                           "QUILL_VIDEO_RETENTION_DAYS": "-1"})
    assert s.data_dir == tmp_path and s.max_upload_bytes == 1234
    assert s.public_url == "https://quill.example.com" and s.cookie_secure
    assert s.stt_model == "aer-stt-qwen3" and s.video_retention_days == -1
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.stt_model = "x"


def test_bad_int():
    with pytest.raises(ValueError):
        Settings.from_env({"QUILL_MAX_FRAMES": "lots"})


def test_with_overrides():
    s = Settings()
    t = s.with_overrides({"stt_model": "aer-stt-qwen3", "video_retention_days": "3", "secret_key": "no", "text_model": ""})
    assert t.stt_model == "aer-stt-qwen3" and t.video_retention_days == 3
    assert t.secret_key == "" and t.text_model == s.text_model
    assert s.with_overrides({}) is s
