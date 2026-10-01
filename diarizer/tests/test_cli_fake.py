import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from quill_diarizer import cli


def make_wav(path: Path, seconds: float, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv("QUILL_DIARIZER_FAKE", "1")


def run_cli(args, capsys):
    code = cli.main(args)
    return code, capsys.readouterr().err


def test_fake_mode_contract(tmp_path, fake_env, capsys):
    wav = make_wav(tmp_path / "diar.wav", 120.0)
    out = tmp_path / "turns.json"
    code, stderr = run_cli(["--input", str(wav), "--output", str(out)], capsys)
    assert code == 0
    doc = json.loads(out.read_text())
    assert doc["model"] == "fake/quill-diarizer"
    assert doc["speakers"] == ["S1", "S2", "S3"]
    assert doc["turns"], "expected turns"
    for t in doc["turns"]:
        assert set(t) == {"speaker", "start", "end", "overlap"}
        assert 0.0 <= t["start"] < t["end"] <= 120.0
        assert t["end"] - t["start"] >= 0.3 - 1e-6
        assert t["speaker"] in doc["speakers"]
    assert any(t["overlap"] for t in doc["turns"])
    assert doc["warnings"] == []
    assert doc["duration"] == pytest.approx(120.0)
    prog = [line for line in stderr.splitlines() if line.startswith("PROGRESS ")]
    assert prog[0] == "PROGRESS 0.0000" and prog[-1] == "PROGRESS 1.0000"
    assert not out.with_name("turns.json.tmp").exists()


def test_fake_mode_is_deterministic(tmp_path, fake_env, capsys):
    wav = make_wav(tmp_path / "a.wav", 45.0)
    o1, o2 = tmp_path / "1.json", tmp_path / "2.json"
    assert cli.main(["--input", str(wav), "--output", str(o1)]) == 0
    assert cli.main(["--input", str(wav), "--output", str(o2)]) == 0
    assert json.loads(o1.read_text())["turns"] == json.loads(o2.read_text())["turns"]


def test_fake_mode_max_speakers(tmp_path, fake_env, capsys):
    wav = make_wav(tmp_path / "a.wav", 90.0)
    out = tmp_path / "t.json"
    assert cli.main(["--input", str(wav), "--output", str(out), "--max-speakers", "2"]) == 0
    assert json.loads(out.read_text())["speakers"] == ["S1", "S2"]


def test_fake_mode_eight_speakers_warns(tmp_path, fake_env, capsys):
    wav = make_wav(tmp_path / "a.wav", 300.0)
    out = tmp_path / "t.json"
    code, stderr = run_cli(["--input", str(wav), "--output", str(out), "--max-speakers", "8"], capsys)
    assert code == 0
    doc = json.loads(out.read_text())
    assert len(doc["speakers"]) == 8
    assert doc["warnings"] and "all 8" in doc["warnings"][0]
    assert "warning:" in stderr


def test_missing_input_exit_3(tmp_path, fake_env, capsys):
    code, stderr = run_cli(["--input", str(tmp_path / "nope.flac"), "--output", str(tmp_path / "o.json")], capsys)
    assert code == 3
    assert "input not found" in stderr


def test_bad_max_speakers_exit_2(tmp_path, fake_env, capsys):
    wav = make_wav(tmp_path / "a.wav", 5.0)
    code, _ = run_cli(["--input", str(wav), "--output", str(tmp_path / "o.json"), "--max-speakers", "0"], capsys)
    assert code == 2


def test_progress_fd(tmp_path, fake_env, capsys):
    wav = make_wav(tmp_path / "a.wav", 10.0)
    r, w = os.pipe()
    try:
        assert cli.main(["--input", str(wav), "--output", str(tmp_path / "o.json"), "--progress-fd", str(w)]) == 0
    finally:
        os.close(w)
    data = os.read(r, 65536).decode()
    os.close(r)
    assert "PROGRESS 1.0000" in data
    assert "PROGRESS" not in capsys.readouterr().err


def test_fake_mode_does_not_import_torch(tmp_path):
    """Subprocess so torch imported by other tests cannot leak in."""
    wav = make_wav(tmp_path / "a.wav", 20.0)
    code = (
        "import sys; from quill_diarizer import cli; "
        f"rc = cli.main(['--input', {str(wav)!r}, '--output', {str(tmp_path / 'o.json')!r}]); "
        "assert 'torch' not in sys.modules and 'transformers' not in sys.modules; sys.exit(rc)"
    )
    env = dict(os.environ, QUILL_DIARIZER_FAKE="1")
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_console_script_usage_error():
    exe = Path(sys.executable).with_name("quill-diarize")
    if not exe.exists():
        pytest.skip("console script not installed")
    proc = subprocess.run([str(exe)], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "--input" in proc.stderr
