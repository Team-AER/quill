"""Frame analysis stage (video mode; owner: C2). PLAN §4.6.

For each key moment: extract a 1280px JPEG + 320px thumbnail into
``media_dir/frames``, send the frame with ±60 s of transcript and the
``look_for`` hint to VISION_MODEL, store the structured result in ``frames`` (``frame_fts`` is kept in sync by
the db.py triggers).

Resumable: a moment whose frame row is already analysed (``kind`` set) and
whose image still exists is skipped. Frames whose analysis failed are stored
with ``kind`` NULL (shown in the gallery, retried on the next run).
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

from quill.pipeline.llm import (LLMClient, LLMError, estimate_tokens, fmt_ts, load_prompt,
                                render_lines, setting, user_message)

CONTEXT_S = 60.0
CONTEXT_MAX_TOKENS = 3000
LONG_EDGE = 1280
THUMB_EDGE = 320
KINDS = ["slide", "screen", "whiteboard", "people", "demo", "other"]

FRAME_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "title": {"type": "string"},
        "visible_text": {"type": "string"},
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "relevance": {"type": "integer", "enum": [0, 1, 2, 3]},
        "caption": {"type": "string"},
    },
    "required": ["kind", "title", "visible_text", "key_facts", "relevance", "caption"],
    "additionalProperties": False,
}


def context_lines(lines: Sequence[Any], t: float, span: float = CONTEXT_S,
                  max_tokens: int = CONTEXT_MAX_TOKENS) -> list[Any]:
    """Transcript lines overlapping [t-span, t+span], trimmed from the far
    ends (keeping those closest to t) to stay under max_tokens."""
    sel = [ln for ln in lines if (ln["end"] or ln["start"]) >= t - span and ln["start"] <= t + span]
    while sel and sum(estimate_tokens(ln["text"] or "") + 6 for ln in sel) > max_tokens:
        if abs(sel[0]["start"] - t) >= abs(sel[-1]["start"] - t):
            sel.pop(0)
        else:
            sel.pop()
    return sel


def build_prompt(t: float, look_for: str | None, why: str | None, ctx_lines: Sequence[Any]) -> str:
    parts = [f"Frame time: [{fmt_ts(t)}]"]
    if look_for:
        parts.append(f"Hint, what might be on screen: {look_for}")
    if why:
        parts.append(f"Why this moment was picked: {why}")
    parts.append("Transcript around this moment:\n" + (render_lines(ctx_lines) or "(no transcript)"))
    return "\n\n".join(parts)


def normalize_result(d: dict) -> dict:
    kind = d.get("kind") if d.get("kind") in KINDS else "other"
    try:
        rel = int(d.get("relevance", 0))
    except (TypeError, ValueError):
        rel = 0
    facts = [str(f).strip()[:300] for f in (d.get("key_facts") or []) if str(f).strip()][:8]
    return {
        "kind": kind,
        "title": (d.get("title") or "").strip()[:200],
        "visible_text": (d.get("visible_text") or "").strip()[:4000],
        "key_facts": facts,
        "relevance": max(0, min(3, rel)),
        "caption": (d.get("caption") or "").strip()[:600],
    }


def analyse_frame(client: LLMClient, image_path: Path, t: float, look_for: str | None,
                  why: str | None, ctx_lines: Sequence[Any]) -> dict:
    system = load_prompt("vision_system.txt")
    msgs = [{"role": "system", "content": system},
            user_message(build_prompt(t, look_for, why, ctx_lines), [image_path])]
    return normalize_result(client.chat_json(msgs, FRAME_SCHEMA, name="frame_analysis", max_tokens=2048))


# ---------------------------------------------------------------- stage

def _media():
    from quill.pipeline import media  # lazy: owned by C1
    return media


def run(ctx: Any) -> None:
    from quill.stages import StageFailed, StagePaused  # lazy: owned by A

    if ctx.mode != "video":
        ctx.progress(1.0, "skipped (audio)")
        return
    conn = ctx.db()
    try:
        meeting = conn.execute("SELECT * FROM meetings WHERE id=?", (ctx.meeting_id,)).fetchone()
        moments = conn.execute("SELECT * FROM moments WHERE meeting_id=? ORDER BY t, id",
                               (ctx.meeting_id,)).fetchall()
        lines = conn.execute("SELECT speaker, start, end, text FROM transcript_lines "
                             "WHERE meeting_id=? ORDER BY start, id", (ctx.meeting_id,)).fetchall()
        existing = {r["moment_id"]: r for r in conn.execute(
            "SELECT * FROM frames WHERE meeting_id=?", (ctx.meeting_id,)).fetchall()}
        # drop frames of moments that no longer exist (moments were re-run)
        moment_ids = {m["id"] for m in moments}
        stale = [r["id"] for mid, r in existing.items() if mid not in moment_ids]
        if stale:
            with conn:
                conn.executemany("DELETE FROM frames WHERE id=?", [(i,) for i in stale])
    finally:
        conn.close()
    if meeting is None:
        raise StageFailed("meeting not found")

    frames_dir = ctx.media_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    todo = []
    for m in moments:
        row = existing.get(m["id"])
        if row is not None and row["kind"] and row["path"] and Path(row["path"]).exists():
            continue
        todo.append(m)
    total = len(moments)
    done = total - len(todo)
    if not todo:
        ctx.progress(1.0, f"{total} frames analysed")
        return

    media = _media()
    try:
        source = Path(media.source_path(ctx, meeting))
    except Exception:
        proxy = Path(ctx.media_dir) / "proxy.mp4"   # frames from the 480p proxy beat no frames
        if not proxy.exists():
            raise StageFailed("source video is no longer available") from None
        source = proxy
    client = LLMClient.from_settings(ctx.settings, "VISION_MODEL", timeout=60.0, retries=2)
    client.check_model_enabled(force=True)
    concurrency = max(1, int(setting(ctx.settings, "VISION_CONCURRENCY", 2)))

    def work(m: Any) -> tuple[Any, Path, Path, dict | None, float]:
        t = float(m["t"])
        stem = f"m{m['id']}_{int(round(t * 1000))}"
        img, thumb = frames_dir / f"{stem}.jpg", frames_dir / f"{stem}_thumb.jpg"
        if not img.exists() or not thumb.exists():
            media.extract_frame(source, t, img, LONG_EDGE, thumb=thumb, thumb_edge=THUMB_EDGE)
        t0 = time.monotonic()
        try:
            res = analyse_frame(client, img, t, m["look_for"], m["why"], context_lines(lines, t))
        except LLMError:
            res = None
        return m, img, thumb, res, time.monotonic() - t0

    failures = 0
    stopped = False
    latencies: list[float] = []
    paused: Exception | None = None
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(work, m) for m in todo]
        for fut in as_completed(futs):
            try:
                m, img, thumb, res, dt = fut.result()
            except StagePaused as e:
                paused = e
                for f in futs:
                    f.cancel()
                continue
            except Exception as e:  # frame extraction failed for this moment
                failures += 1
                ctx.log(f"frames: extraction failed ({type(e).__name__})")
                continue
            if res is None:
                failures += 1
            else:
                latencies.append(dt)
            _store(ctx, m, img, thumb, res, existing.get(m["id"]))
            done += 1
            ctx.progress(done / total, f"frame {done}/{total}")
            if ctx.should_stop():
                stopped = True
                for f in futs:
                    f.cancel()
                break
    if paused is not None:
        raise paused
    if stopped:
        from quill.pipeline.moments import cancelled
        raise cancelled()
    if latencies:
        ctx.log(f"frames: {len(latencies)} analysed, mean {sum(latencies) / len(latencies):.1f}s per call, "
                f"{failures} failed")
    if failures and failures > len(todo) / 2:
        raise StageFailed(f"frame analysis failed for {failures} of {len(todo)} frames")
    ctx.progress(1.0, f"{total - failures} frames analysed")


def _store(ctx: Any, m: Any, img: Path, thumb: Path, res: dict | None,
           old: Any) -> None:
    conn = ctx.db()
    try:
        with conn:
            conn.execute("DELETE FROM frames WHERE meeting_id=? AND moment_id=?", (ctx.meeting_id, m["id"]))
            r = res or {}
            conn.execute(
                "INSERT INTO frames(meeting_id, moment_id, t, path, thumb_path, kind, title, visible_text,"
                " key_facts, relevance, caption) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ctx.meeting_id, m["id"], float(m["t"]), str(img), str(thumb) if thumb.exists() else None,
                 r.get("kind"), r.get("title"), r.get("visible_text"),
                 json.dumps(r.get("key_facts", [])) if res else None,
                 r.get("relevance"), r.get("caption")))
    finally:
        conn.close()
