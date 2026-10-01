"""Key-moment detection + fusion stage (video mode only; owner: C2). PLAN §4.5.

1. Text candidates: the text model reads ~15-min transcript windows and returns
   ``{t, why, look_for}`` items where something is shown or referenced visually.
2. Pixel candidates: ``media.scene_changes`` (ffmpeg scene score).
3. Fusion (pure functions below): snap text moments to the nearest scene change
   within ±20 s, keep long-lived scenes (>30 s) without text, dedupe by pHash
   (hamming ≤ 6), then apply the frame budget (FRAMES_PER_HOUR, capped at
   MAX_FRAMES), text-backed moments first.
4. No transcript: scene changes + a fixed 1-per-3-min sample.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from quill.pipeline.llm import (LLMClient, LLMError, estimate_tokens, load_prompt, parse_ts,
                                render_lines, setting)

TEXT_WINDOW_S = 15 * 60
WINDOW_MAX_TOKENS = 12000       # transcript tokens per call (well under the 131k context)
SNAP_WINDOW_S = 20.0
LONG_SCENE_S = 30.0
CLUSTER_GAP_S = 4.0             # scene changes closer than this form one cluster (animations, scrolling)
SETTLE_S = 1.5                  # take the frame this long after a cut, not on the transition
SAMPLE_EVERY_S = 180.0
PHASH_MAX_DIST = 6
SCENE_THRESHOLD = 0.3
MAX_ITEMS_PER_WINDOW = 12

MOMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "t": {"type": "string"},
                    "why": {"type": "string"},
                    "look_for": {"type": "string"},
                },
                "required": ["t", "why", "look_for"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}


@dataclass
class Candidate:
    t: float
    source: str                 # "text" | "text+scene" | "scene" | "sample"
    why: str = ""
    look_for: str = ""
    priority: int = 0           # 2 = text-backed, 1 = scene, 0 = fixed sample
    weight: float = 0.0         # tie-breaker within a priority (scene duration, ...)
    phash: str | None = None
    scene_start: float | None = field(default=None, repr=False)


# ---------------------------------------------------------------- windows

def split_windows(lines: Sequence[Any], window_s: float = TEXT_WINDOW_S,
                  max_tokens: int = WINDOW_MAX_TOKENS) -> list[list[Any]]:
    """Split transcript lines into windows of ~window_s seconds at line
    boundaries; a window is also closed early when it would exceed max_tokens."""
    windows: list[list[Any]] = []
    cur: list[Any] = []
    cur_start = None
    cur_tokens = 0
    for ln in lines:
        text = (ln["text"] or "").strip()
        if not text:
            continue
        tok = estimate_tokens(text) + 6
        if cur and (ln["start"] - cur_start >= window_s or cur_tokens + tok > max_tokens):
            windows.append(cur)
            cur, cur_tokens = [], 0
        if not cur:
            cur_start = ln["start"]
        cur.append(ln)
        cur_tokens += tok
    if cur:
        windows.append(cur)
    return windows


# ---------------------------------------------------------------- scenes

def normalize_scene_changes(raw: Iterable[Any]) -> list[float]:
    """Accept floats, (t, score) tuples or {"t": ...} dicts; return sorted times."""
    out: list[float] = []
    for item in raw or []:
        if isinstance(item, dict):
            t = item.get("t", item.get("time", item.get("pts_time")))
        elif isinstance(item, (tuple, list)):
            t = item[0] if item else None
        else:
            t = item
        try:
            tf = float(t)
        except (TypeError, ValueError):
            continue
        if math.isfinite(tf) and tf >= 0:
            out.append(tf)
    return sorted(set(out))


def scene_spans(changes: Sequence[float], duration: float,
                cluster_gap: float = CLUSTER_GAP_S) -> list[tuple[float, float]]:
    """Group scene changes into clusters and return the stable spans between
    them as (start, end). Recording start counts as a boundary."""
    bounds: list[tuple[float, float]] = [(0.0, 0.0)]  # (cluster_start, cluster_end)
    for t in changes:
        if t <= 0:
            continue
        cs, ce = bounds[-1]
        if t - ce < cluster_gap:
            bounds[-1] = (cs, t)
        else:
            bounds.append((t, t))
    spans = []
    for i, (_, ce) in enumerate(bounds):
        end = bounds[i + 1][0] if i + 1 < len(bounds) else duration
        if end > ce:
            spans.append((ce, end))
    return spans


def fuse(text_moments: Sequence[dict], scene_changes: Sequence[float], duration: float, *,
         snap_s: float = SNAP_WINDOW_S, long_scene_s: float = LONG_SCENE_S,
         settle_s: float = SETTLE_S, same_view_s: float = 60.0) -> list[Candidate]:
    """Fuse text moments with scene changes (pure).

    - A text moment snaps to the nearest scene change within ±snap_s (the frame
      is taken settle_s after the cut); otherwise it keeps its own time.
    - Each stable scene span longer than long_scene_s becomes a scene candidate
      unless a text moment already landed in it.
    - Text moments in the same scene span less than same_view_s apart are merged
      (their `why`s joined); a text-backed span suppresses its scene candidate.
    """
    changes = sorted(scene_changes)
    spans = scene_spans(changes, duration)

    def span_of(t: float) -> tuple[float, float] | None:
        for s, e in spans:
            if s <= t < e:
                return (s, e)
        return None

    def clamp(t: float) -> float:
        return max(0.0, min(t, max(0.0, duration - 0.5)))

    by_span: dict[Any, list[Candidate]] = {}
    loose: list[Candidate] = []
    for m in text_moments:
        t = m.get("t")
        if t is None or not (0 <= t <= duration + 1):
            continue
        near = min(changes, key=lambda c: abs(c - t)) if changes else None
        if near is not None and abs(near - t) <= snap_s:
            # the moment refers to what appeared at that cut: look just after it settles
            sp = span_of(near + 1e-6) or next(((a, b) for a, b in spans if a >= near), None)
            anchor = sp[0] if sp else near
            tt = clamp(anchor + settle_s)
            if sp and sp[1] - sp[0] < settle_s * 2:
                tt = clamp((sp[0] + sp[1]) / 2)
            cand = Candidate(t=tt, source="text+scene", why=m.get("why", ""),
                             look_for=m.get("look_for", ""), priority=2, scene_start=anchor)
        else:
            cand = Candidate(t=clamp(t), source="text", why=m.get("why", ""),
                             look_for=m.get("look_for", ""), priority=2)
        sp = span_of(cand.t)
        if sp is None:
            loose.append(cand)
            continue
        group = by_span.setdefault(sp, [])
        prev = next((p for p in group if abs(p.t - cand.t) < same_view_s), None)
        if prev is None:
            group.append(cand)
        else:
            prev.why = _join(prev.why, cand.why)
            prev.look_for = _join(prev.look_for, cand.look_for)
            if "scene" in cand.source and prev.source == "text":
                prev.source, prev.t = "text+scene", cand.t
    for s, e in spans:
        if e - s > long_scene_s and (s, e) not in by_span:
            by_span[(s, e)] = [Candidate(t=clamp(s + min(settle_s * 2, (e - s) / 2)), source="scene",
                                         priority=1, weight=e - s, scene_start=s)]
    # text moments with no scene span (no scene data) still dedupe by proximity
    out = [c for g in by_span.values() for c in g] + _dedupe_close(loose, gap=snap_s / 2)
    out.sort(key=lambda c: c.t)
    for c in out:
        if c.priority == 2 and not c.weight:
            c.weight = 1.0
    return out


def fallback_candidates(scene_changes: Sequence[float], duration: float,
                        every_s: float = SAMPLE_EVERY_S, settle_s: float = SETTLE_S) -> list[Candidate]:
    """No transcript: every scene span (any length) + a fixed 1-per-every_s sample."""
    spans = scene_spans(sorted(scene_changes), duration)
    out = [Candidate(t=min(s + settle_s, max(0.0, (s + e) / 2)), source="scene", priority=1,
                     weight=e - s, scene_start=s) for s, e in spans if e - s >= 2.0]
    t = min(every_s / 2, duration / 2)
    while t < duration:
        if not any(abs(c.t - t) < every_s / 6 for c in out):
            out.append(Candidate(t=t, source="sample", priority=0))
        t += every_s
    out.sort(key=lambda c: c.t)
    return out


def frame_budget(duration: float, frames_per_hour: float, max_frames: int) -> int:
    return max(1, min(int(max_frames), int(math.ceil(frames_per_hour * max(duration, 1) / 3600.0))))


def hamming(a: Any, b: Any) -> int:
    ia = int(a, 16) if isinstance(a, str) else int(a)
    ib = int(b, 16) if isinstance(b, str) else int(b)
    return bin(ia ^ ib).count("1")


def priority_order(cands: Sequence[Candidate], budget: int) -> list[Candidate]:
    """Order candidates for selection: text-backed first; within a priority,
    spread over time (if there are more than the budget, pick evenly spaced
    ones first), scene ones by longest scene first."""
    out: list[Candidate] = []
    for prio in (2, 1, 0):
        group = [c for c in cands if c.priority == prio]
        if prio == 1:
            group.sort(key=lambda c: (-c.weight, c.t))
        else:
            group.sort(key=lambda c: c.t)
            group = _spread(group)
        out.extend(group)
    return out


def select(cands: Sequence[Candidate], budget: int,
           hash_fn: Callable[[Candidate], Any] | None = None,
           max_dist: int = PHASH_MAX_DIST) -> list[Candidate]:
    """Walk candidates in priority order, dropping near-duplicate frames
    (pHash hamming ≤ max_dist) until the budget is filled. Returns the
    kept candidates sorted by time. hash_fn may return None (unknown)."""
    kept: list[Candidate] = []
    hashes: list[Any] = []
    for c in priority_order(cands, budget):
        if len(kept) >= budget:
            break
        h = hash_fn(c) if hash_fn else c.phash
        if h is not None:
            dup = next((i for i, k in enumerate(hashes) if k is not None and hamming(h, k) <= max_dist), None)
            if dup is not None:
                if c.priority == 2 and kept[dup].priority == 2:
                    kept[dup].why = _join(kept[dup].why, c.why)
                    kept[dup].look_for = _join(kept[dup].look_for, c.look_for)
                continue
        c = replace(c, phash=_hash_str(h))
        kept.append(c)
        hashes.append(h)
    kept.sort(key=lambda c: c.t)
    return kept


def _spread(group: list[Candidate]) -> list[Candidate]:
    """Reorder a time-sorted list so any prefix is spread over the timeline
    (binary subdivision order: middle, quarters, eighths, ...)."""
    n = len(group)
    if n <= 2:
        return group
    order: list[int] = []
    seen: set[int] = set()
    k = 1
    while len(order) < n:
        for j in range(k):
            i = int((j + 0.5) * n / k)
            i = min(i, n - 1)
            if i not in seen:
                seen.add(i)
                order.append(i)
        k *= 2
        if k > 4 * n:
            order.extend(i for i in range(n) if i not in seen)
            break
    return [group[i] for i in order]


def _dedupe_close(cands: list[Candidate], gap: float) -> list[Candidate]:
    out: list[Candidate] = []
    for c in sorted(cands, key=lambda c: c.t):
        if out and c.t - out[-1].t < gap:
            out[-1].why = _join(out[-1].why, c.why)
            out[-1].look_for = _join(out[-1].look_for, c.look_for)
        else:
            out.append(c)
    return out


def _join(a: str, b: str) -> str:
    a, b = (a or "").strip(), (b or "").strip()
    if not b or b in a:
        return a
    if not a:
        return b
    return f"{a}; {b}"[:500]


def _hash_str(h: Any) -> str | None:
    if h is None:
        return None
    return h if isinstance(h, str) else format(int(h), "016x")


# ---------------------------------------------------------------- LLM text candidates

def text_candidates(client: LLMClient, lines: Sequence[Any], *, concurrency: int = 2,
                    on_window: Callable[[int, int], None] | None = None,
                    should_stop: Callable[[], bool] | None = None) -> list[dict]:
    windows = split_windows(lines)
    system = load_prompt("moments_system.txt").replace("{max_items}", str(MAX_ITEMS_PER_WINDOW))
    results: list[list[dict]] = [[] for _ in windows]
    done = 0

    def one(i: int) -> list[dict]:
        w = windows[i]
        lo, hi = w[0]["start"], max(ln["end"] or ln["start"] for ln in w)
        msg = f"Transcript window:\n\n{render_lines(w)}"
        data = client.chat_json([{"role": "system", "content": system},
                                 {"role": "user", "content": msg}],
                                MOMENT_SCHEMA, name="key_moments", max_tokens=2048)
        out = []
        for item in data.get("moments", [])[:MAX_ITEMS_PER_WINDOW * 2]:
            t = parse_ts(item.get("t"))
            if t is None or t < lo - 30 or t > hi + 30:
                continue  # hallucinated or out-of-window timestamp
            out.append({"t": t, "why": (item.get("why") or "")[:300],
                        "look_for": (item.get("look_for") or "")[:200]})
        return out

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futs = {pool.submit(one, i): i for i in range(len(windows))}
        for fut in futs:
            i = futs[fut]
            try:
                results[i] = fut.result()
            except LLMError:
                results[i] = []   # one bad window must not sink the stage; scenes still cover it
            done += 1
            if on_window:
                on_window(done, len(windows))
            if should_stop and should_stop():
                for f in futs:
                    f.cancel()
                break
    return [m for r in results for m in r]


# ---------------------------------------------------------------- stage

def _media():
    from quill.pipeline import media  # lazy: owned by C1
    return media


def _source_path(ctx: Any, meeting: Any, media: Any) -> Path | None:
    """Prefer a proxy.mp4 (smaller decode) when C1 made one, else the source."""
    proxy = Path(ctx.media_dir) / "proxy.mp4"
    if proxy.exists():
        return proxy
    try:
        return Path(media.source_path(ctx, meeting))
    except Exception:
        return None


def cancelled() -> Exception:
    try:
        from quill.pipeline.media import Cancelled
        return Cancelled()
    except Exception:  # pragma: no cover
        from quill.stages import StageFailed
        return StageFailed("cancelled")


def run(ctx: Any) -> None:
    from quill.stages import StageFailed  # lazy: owned by A

    conn = ctx.db()
    try:
        meeting = conn.execute("SELECT * FROM meetings WHERE id=?", (ctx.meeting_id,)).fetchone()
        lines = conn.execute(
            "SELECT speaker, start, end, text FROM transcript_lines WHERE meeting_id=? ORDER BY start, id",
            (ctx.meeting_id,)).fetchall()
    finally:
        conn.close()
    if meeting is None:
        raise StageFailed("meeting not found")
    if ctx.mode != "video":
        ctx.log("key_moments skipped: audio mode")
        ctx.progress(1.0, "skipped (audio)")
        return
    media = _media()
    source = _source_path(ctx, meeting, media)
    if source is None:
        raise StageFailed("source video is no longer available")
    duration = float(meeting["duration_s"] or 0) or (max((ln["end"] or 0) for ln in lines) if lines else 0)
    s = ctx.settings
    budget = frame_budget(duration, float(setting(s, "FRAMES_PER_HOUR", 40)), int(setting(s, "MAX_FRAMES", 200)))

    have_text = any((ln["text"] or "").strip() for ln in lines)
    text_moments: list[dict] = []
    if have_text:
        client = LLMClient.from_settings(s, "TEXT_MODEL", timeout=120.0)
        client.check_model_enabled(force=True)
        ctx.progress(0.02, "reading transcript for visual references")
        text_moments = text_candidates(
            client, lines, concurrency=int(setting(s, "VISION_CONCURRENCY", 2)),
            on_window=lambda d, n: ctx.progress(0.05 + 0.45 * d / max(n, 1), f"transcript window {d}/{n}"),
            should_stop=ctx.should_stop)
        if ctx.should_stop():
            raise cancelled()
    ctx.progress(0.5, "detecting scene changes")
    changes = normalize_scene_changes(media.scene_changes(
        source, SCENE_THRESHOLD, should_stop=ctx.should_stop, duration=duration or None,
        progress=lambda f: ctx.progress(0.5 + 0.2 * max(0.0, min(1.0, f)), "detecting scene changes")))
    ctx.log(f"key_moments: {len(text_moments)} text candidates, {len(changes)} scene changes, budget {budget}")

    if have_text:
        cands = fuse(text_moments, changes, duration)
    else:
        cands = fallback_candidates(changes, duration)

    probe_dir = ctx.media_dir / "frames" / "_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    counter = {"n": 0}
    limit = max(budget * 3, budget + 20)

    def hash_fn(c: Candidate) -> Any:
        counter["n"] += 1
        if counter["n"] > limit or ctx.should_stop():
            return None
        ctx.progress(min(0.98, 0.7 + 0.28 * counter["n"] / limit), "deduplicating frames")
        out = probe_dir / f"p_{int(c.t * 1000)}.jpg"
        try:
            media.extract_frame(source, c.t, out, 320)
            return media.phash(out)
        except Exception:
            return None

    ctx.progress(0.7, "deduplicating frames")
    kept = select(cands, budget, hash_fn)
    for p in probe_dir.glob("p_*.jpg"):
        p.unlink(missing_ok=True)
    try:
        probe_dir.rmdir()
    except OSError:
        pass
    if ctx.should_stop():
        raise cancelled()

    conn = ctx.db()
    try:
        with conn:
            conn.execute("DELETE FROM moments WHERE meeting_id=?", (ctx.meeting_id,))
            conn.executemany(
                "INSERT INTO moments(meeting_id, t, source, why, look_for, phash) VALUES (?,?,?,?,?,?)",
                [(ctx.meeting_id, round(c.t, 3), c.source, c.why or None, c.look_for or None, c.phash)
                 for c in kept])
    finally:
        conn.close()
    ctx.log(f"key_moments: kept {len(kept)} moments")
    ctx.progress(1.0, f"{len(kept)} key moments")
