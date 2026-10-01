"""Stage ``diarize``: run the diarizer subprocess (CONTRACT "Diarizer CLI") and
store its turns + speakers.

    quill-diarize --input diar.flac --output turns.json [--max-speakers N] [--threads N]

stderr carries ``PROGRESS <fraction>`` lines (forwarded to ctx.progress); any
other stderr line starting with ``WARNING``/``WARN`` is stored as an event, as
are entries of an optional ``"warnings"`` list in turns.json. Exit != 0 fails
the stage with the last stderr lines.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from quill.pipeline.catalog import setting
from quill.pipeline.media import run_process
from quill.stages import StageFailed

MAX_SLOTS = 8
_PROGRESS = re.compile(r"^\s*PROGRESS\s+([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)\s*$")
_WARNING = re.compile(r"^\s*(?:WARNING|WARN)\b[:\s-]*(.*)$", re.IGNORECASE)
_LABEL = re.compile(r"^S([1-9]\d*)$")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def build_cmd(settings: Any, flac: Path, out: Path, max_speakers: int | None) -> list[str]:
    cmd = shlex.split(str(setting(settings, "DIARIZER_CMD", "quill-diarize")))
    cmd += ["--input", str(flac), "--output", str(out)]
    if max_speakers:
        cmd += ["--max-speakers", str(max(1, min(MAX_SLOTS, int(max_speakers))))]
    threads = setting(settings, "DIARIZER_THREADS")
    if threads:
        cmd += ["--threads", str(int(threads))]
    return cmd


def parse_turns(payload: dict[str, Any], duration: float | None = None) -> tuple[list[dict], list[str]]:
    """Validate turns.json. Returns (turns, speaker_labels). Labels that are not
    ``S<n>`` are renamed S1.. in order of first appearance."""
    if not isinstance(payload, dict) or not isinstance(payload.get("turns"), list):
        raise StageFailed("diarizer output has no turns list")
    raw_turns = []
    for t in payload["turns"]:
        try:
            spk = str(t["speaker"])
            start, end = float(t["start"]), float(t["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (start == start and end == end) or end <= start:
            continue
        if duration:
            start, end = max(0.0, start), min(float(duration), end)
            if end <= start:
                continue
        raw_turns.append((spk, max(0.0, start), end, bool(t.get("overlap", False))))
    raw_turns.sort(key=lambda x: (x[1], x[2]))
    declared = [str(s) for s in payload.get("speakers") or []]
    seen = list(dict.fromkeys(declared + [t[0] for t in raw_turns]))
    if all(_LABEL.match(s) for s in seen):
        mapping = {s: s for s in seen}
        ordered = sorted(seen, key=lambda s: int(s[1:]))
    else:
        order = list(dict.fromkeys([t[0] for t in raw_turns] + declared))
        mapping = {s: f"S{i + 1}" for i, s in enumerate(order)}
        ordered = [mapping[s] for s in order]
    # Only keep speakers that actually speak (plus declared ones if no turns at all).
    used = {mapping[t[0]] for t in raw_turns}
    labels = [s for s in ordered if s in used] if used else []
    turns = [
        {"speaker": mapping[s], "start": round(a, 3), "end": round(b, 3), "overlap": o}
        for s, a, b, o in raw_turns
    ]
    return turns, labels


def store(conn, meeting_id: str, turns: list[dict], labels: list[str]) -> None:
    """Replace turns and speakers for a meeting (in the caller's transaction)."""
    conn.execute("DELETE FROM turns WHERE meeting_id=?", (meeting_id,))
    conn.execute("DELETE FROM speakers WHERE meeting_id=?", (meeting_id,))
    conn.executemany(
        "INSERT INTO turns(meeting_id, speaker, start, end, overlap) VALUES (?,?,?,?,?)",
        [(meeting_id, t["speaker"], t["start"], t["end"], 1 if t["overlap"] else 0) for t in turns],
    )
    conn.executemany(
        "INSERT INTO speakers(meeting_id, label, display_name, color) VALUES (?,?,?,?)",
        [(meeting_id, label, None, i % MAX_SLOTS) for i, label in enumerate(labels)],
    )


def add_event(conn, meeting_id: str, kind: str, message: str) -> None:
    conn.execute(
        "INSERT INTO events(meeting_id, at, kind, message) VALUES (?,?,?,?)",
        (meeting_id, _now(), kind, message[:1000]),
    )


def run(ctx: Any) -> None:
    media_dir = Path(ctx.media_dir)
    flac = media_dir / "diar.flac"
    if not flac.exists():
        raise StageFailed("diar.flac is missing; re-run audio extraction")
    conn = ctx.db()
    try:
        row = conn.execute(
            "SELECT expected_speakers, duration_s FROM meetings WHERE id=?", (ctx.meeting_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise StageFailed("meeting not found")
    options = getattr(ctx, "options", None) or {}
    expected = options.get("expected_speakers") or options.get("max_speakers") or row["expected_speakers"]
    try:
        expected = int(expected) if expected else None
    except (TypeError, ValueError):
        expected = None
    duration = row["duration_s"]

    out = media_dir / "turns.json"
    tmp = media_dir / "turns.json.part"
    tmp.unlink(missing_ok=True)
    warnings: list[str] = []

    def on_stderr(line: str) -> None:
        m = _PROGRESS.match(line)
        if m:
            ctx.progress(min(1.0, max(0.0, float(m.group(1)))) * 0.98, "diarizing")
            return
        w = _WARNING.match(line)
        if w:
            warnings.append(w.group(1).strip() or line.strip())

    ctx.progress(0.0, "diarizing")
    cmd = build_cmd(ctx.settings, flac, tmp, expected)
    ctx.log(f"diarize: starting {Path(cmd[0]).name} max_speakers={expected or 'auto'}")
    try:
        code, _, err = run_process(cmd, should_stop=ctx.should_stop, on_stderr=on_stderr, env=os.environ.copy())
    except FileNotFoundError as exc:
        raise StageFailed(f"diarizer not found: {cmd[0]}") from exc
    if code != 0:
        tmp.unlink(missing_ok=True)
        tail = " | ".join(l.strip() for l in err if l.strip() and not _PROGRESS.match(l))
        raise StageFailed(f"diarizer failed (exit {code}): {tail[-500:] or 'no message'}")
    try:
        payload = json.loads(tmp.read_text())
    except (OSError, ValueError) as exc:
        raise StageFailed("diarizer produced no readable turns.json") from exc
    os.replace(tmp, out)
    turns, labels = parse_turns(payload, duration)
    for w in payload.get("warnings") or []:
        warnings.append(str(w))
    if len(labels) >= MAX_SLOTS:
        warnings.append(
            f"all {MAX_SLOTS} speaker slots are in use; similar voices may have been merged"
        )
    if not turns:
        warnings.append("no speech detected by the diarizer")
    conn = ctx.db()
    try:
        with conn:
            store(conn, ctx.meeting_id, turns, labels)
            for w in dict.fromkeys(warnings):
                add_event(conn, ctx.meeting_id, "warning", f"diarize: {w}")
    finally:
        conn.close()
    ctx.log(f"diarize: {len(turns)} turns, {len(labels)} speakers, model={payload.get('model')}")
    ctx.progress(1.0, f"{len(labels)} speakers")
