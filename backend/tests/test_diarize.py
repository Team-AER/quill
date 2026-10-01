"""diarize stage against fake diarizer executables implementing the CLI contract."""

from __future__ import annotations

import json
import shlex
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fixtures.c1_helpers import FakeCtx, FakeSettings  # noqa: E402

from quill.pipeline import diarize  # noqa: E402
from quill.pipeline.media import Cancelled  # noqa: E402
from quill.stages import StageFailed  # noqa: E402

FAKE = textwrap.dedent(
    """
    import argparse, json, sys, time
    p = argparse.ArgumentParser()
    p.add_argument("--input"); p.add_argument("--output")
    p.add_argument("--max-speakers", type=int); p.add_argument("--threads", type=int)
    a = p.parse_args()
    mode = {mode!r}
    json.dump({{"argv": sys.argv[1:]}}, open(a.output + ".argv", "w"))
    if mode == "fail":
        print("PROGRESS 0.1", file=sys.stderr)
        print("RuntimeError: out of memory", file=sys.stderr)
        sys.exit(3)
    if mode == "slow":
        time.sleep(30)
    for f in (0.25, 0.5, 1.0):
        print(f"PROGRESS {{f}}", file=sys.stderr, flush=True)
    print("WARNING: audio is very quiet", file=sys.stderr)
    print("some other log line", file=sys.stderr)
    turns = {turns}
    json.dump({{"model": "fake", "speakers": {speakers}, "turns": turns, "warnings": {warnings}}},
              open(a.output, "w"))
    """
)

DEFAULT_TURNS = [
    {"speaker": "S2", "start": 3.0, "end": 5.0, "overlap": False},
    {"speaker": "S1", "start": 0.0, "end": 3.2, "overlap": True},
    {"speaker": "S1", "start": 6.0, "end": 5.0, "overlap": False},  # invalid, dropped
]


def fake_cmd(tmp: Path, mode="ok", turns=None, speakers=("S1", "S2"), warnings=()) -> str:
    script = tmp / f"fake_diarizer_{mode}.py"
    script.write_text(
        FAKE.format(
            mode=mode,
            turns=repr(turns if turns is not None else DEFAULT_TURNS),
            speakers=repr(list(speakers)),
            warnings=repr(list(warnings)),
        )
    )
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def make_ctx(tmp_path, cmd, **meeting) -> FakeCtx:
    ctx = FakeCtx(tmp_path, settings=FakeSettings(data_dir=tmp_path, diarizer_cmd=cmd), **meeting)
    (ctx.media_dir / "diar.flac").write_bytes(b"fLaC")
    return ctx


def test_run_stores_turns_speakers_progress_and_warnings(tmp_path):
    ctx = make_ctx(tmp_path, fake_cmd(tmp_path, warnings=["from json"]), expected_speakers=3)
    diarize.run(ctx)
    turns = ctx.q("SELECT speaker, start, end, overlap FROM turns WHERE meeting_id='m1' ORDER BY start")
    assert [tuple(r) for r in turns] == [("S1", 0.0, 3.2, 1), ("S2", 3.0, 5.0, 0)]
    sp = ctx.q("SELECT label, color FROM speakers WHERE meeting_id='m1' ORDER BY label")
    assert [tuple(r) for r in sp] == [("S1", 0), ("S2", 1)]
    fr = [f for f, d in ctx.progress_calls if d == "diarizing"]
    assert fr[0] == 0.0 and max(fr) >= 0.9
    assert ctx.progress_calls[-1][0] == 1.0
    events = [r["message"] for r in ctx.q("SELECT message FROM events WHERE kind='warning'")]
    assert "diarize: audio is very quiet" in events
    assert "diarize: from json" in events
    argv = json.loads((ctx.media_dir / "turns.json.part.argv").read_text())["argv"]
    assert argv[argv.index("--max-speakers") + 1] == "3"
    assert (ctx.media_dir / "turns.json").exists()


def test_rerun_replaces_rows(tmp_path):
    ctx = make_ctx(tmp_path, fake_cmd(tmp_path))
    diarize.run(ctx)
    ctx.exec("UPDATE speakers SET display_name='Alice' WHERE label='S1'")
    one = [{"speaker": "S1", "start": 0.0, "end": 9.0, "overlap": False}]
    ctx.settings.diarizer_cmd = fake_cmd(tmp_path, mode="ok2", turns=one, speakers=["S1"])
    # mode "ok2" behaves like ok (only "fail"/"slow" are special)
    diarize.run(ctx)
    assert len(ctx.q("SELECT * FROM turns")) == 1
    assert [r["label"] for r in ctx.q("SELECT label FROM speakers")] == ["S1"]
    argv = json.loads((ctx.media_dir / "turns.json.part.argv").read_text())["argv"]
    assert "--max-speakers" not in argv


def test_rerun_option_overrides_expected_speakers(tmp_path):
    ctx = make_ctx(tmp_path, fake_cmd(tmp_path), expected_speakers=5)
    ctx.options = {"expected_speakers": 2}
    diarize.run(ctx)
    argv = json.loads((ctx.media_dir / "turns.json.part.argv").read_text())["argv"]
    assert argv[argv.index("--max-speakers") + 1] == "2"


def test_failure_surfaces_stderr(tmp_path):
    ctx = make_ctx(tmp_path, fake_cmd(tmp_path, mode="fail"))
    with pytest.raises(StageFailed, match="out of memory") as ei:
        diarize.run(ctx)
    assert "exit 3" in ei.value.message
    assert "PROGRESS" not in ei.value.message


def test_missing_executable(tmp_path):
    ctx = make_ctx(tmp_path, str(tmp_path / "nope"))
    with pytest.raises(StageFailed, match="not found"):
        diarize.run(ctx)


def test_missing_flac(tmp_path):
    ctx = FakeCtx(tmp_path)
    with pytest.raises(StageFailed, match="diar.flac"):
        diarize.run(ctx)


def test_cancel_kills_diarizer(tmp_path):
    ctx = make_ctx(tmp_path, fake_cmd(tmp_path, mode="slow"))
    n = {"i": 0}

    def stop():
        n["i"] += 1
        return n["i"] > 3

    ctx.should_stop = stop
    with pytest.raises(Cancelled):
        diarize.run(ctx)


def test_eight_speakers_warns(tmp_path):
    turns = [{"speaker": f"S{i}", "start": i * 2.0, "end": i * 2.0 + 1.5} for i in range(1, 9)]
    ctx = make_ctx(tmp_path, fake_cmd(tmp_path, turns=turns, speakers=[f"S{i}" for i in range(1, 9)]))
    diarize.run(ctx)
    assert [r["color"] for r in ctx.q("SELECT color FROM speakers ORDER BY color")] == list(range(8))
    assert any("8 speaker slots" in r["message"] for r in ctx.q("SELECT message FROM events"))


def test_parse_turns_renames_foreign_labels_and_clamps():
    payload = {
        "speakers": ["spk_a", "spk_b"],
        "turns": [
            {"speaker": "spk_b", "start": 0.0, "end": 1.0},
            {"speaker": "spk_a", "start": 1.0, "end": 12.0},
            {"speaker": "spk_c", "start": "bad", "end": 3},
        ],
    }
    turns, labels = diarize.parse_turns(payload, duration=10.0)
    assert labels == ["S1", "S2"]
    assert turns[0]["speaker"] == "S1" and turns[1] == {
        "speaker": "S2", "start": 1.0, "end": 10.0, "overlap": False
    }


def test_parse_turns_sorts_numeric_labels():
    turns, labels = diarize.parse_turns(
        {"turns": [{"speaker": "S10", "start": 0, "end": 1}, {"speaker": "S2", "start": 1, "end": 2}]}
    )
    assert labels == ["S2", "S10"]


def test_parse_turns_rejects_garbage():
    with pytest.raises(StageFailed):
        diarize.parse_turns({"nope": 1})


def test_build_cmd_clamps_and_threads():
    s = FakeSettings(diarizer_cmd="/opt/x/quill-diarize --flag", diarizer_threads=6)
    cmd = diarize.build_cmd(s, Path("a.flac"), Path("t.json"), 12)
    assert cmd[:2] == ["/opt/x/quill-diarize", "--flag"]
    assert cmd[cmd.index("--max-speakers") + 1] == "8"
    assert cmd[cmd.index("--threads") + 1] == "6"
