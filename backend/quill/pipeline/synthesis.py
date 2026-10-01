"""Map-reduce notes synthesis stage (owner: C2). PLAN §4.7, CONTRACT "Notes JSON".

Map: one call per ~20-min window (split at line boundaries, capped by tokens)
with the frame analyses inside the window -> section title/summary, decisions,
action items, open questions, speaker-name evidence (each with a timestamp).
Map results are cached on disk (media_dir/synthesis/) so a restart of an 8 h
meeting does not redo finished windows.

Reduce: one call merges the section items by id; merged items keep the
timestamp of their earliest source item, so no timestamp is ever produced by
the reduce step. Every decision/action/question is then grounded against the
transcript (t inside the meeting, lexical overlap with lines within ±90 s).

Budget: each map call carries <= ~14k transcript tokens; the reduce input is
kept under ~40k tokens; both are far below Gemma's 131072 context.
"""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Sequence

from quill.pipeline.llm import (LLMClient, LLMError, estimate_tokens, fmt_ts, load_prompt,
                                parse_ts, render_lines, setting)
from quill.pipeline.moments import split_windows

PROMPT_VERSION = "synth-v1"
MAP_WINDOW_S = 20 * 60
MAP_MAX_TOKENS = 14000
REDUCE_MAX_TOKENS = 40000
GROUND_WINDOW_S = 90.0
MAX_KEY_VISUALS = 30
FRAME_TEXT_CHARS = 400

_TS_STR = {"type": "string"}
MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {
            "type": "object", "properties": {"text": {"type": "string"}, "t": _TS_STR},
            "required": ["text", "t"], "additionalProperties": False}},
        "action_items": {"type": "array", "items": {
            "type": "object",
            "properties": {"owner": {"type": "string"}, "task": {"type": "string"},
                           "due": {"type": "string"}, "t": _TS_STR},
            "required": ["owner", "task", "due", "t"], "additionalProperties": False}},
        "open_questions": {"type": "array", "items": {
            "type": "object", "properties": {"text": {"type": "string"}, "t": _TS_STR},
            "required": ["text", "t"], "additionalProperties": False}},
        "speaker_names": {"type": "array", "items": {
            "type": "object",
            "properties": {"label": {"type": "string"}, "name": {"type": "string"},
                           "evidence_t": _TS_STR},
            "required": ["label", "name", "evidence_t"], "additionalProperties": False}},
    },
    "required": ["title", "summary", "decisions", "action_items", "open_questions", "speaker_names"],
    "additionalProperties": False,
}

_IDS = {"type": "array", "items": {"type": "string"}}
REDUCE_SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {
            "type": "object", "properties": {"text": {"type": "string"}, "source_ids": _IDS},
            "required": ["text", "source_ids"], "additionalProperties": False}},
        "action_items": {"type": "array", "items": {
            "type": "object",
            "properties": {"owner": {"type": "string"}, "task": {"type": "string"},
                           "due": {"type": "string"}, "source_ids": _IDS},
            "required": ["owner", "task", "due", "source_ids"], "additionalProperties": False}},
        "open_questions": {"type": "array", "items": {
            "type": "object", "properties": {"text": {"type": "string"}, "source_ids": _IDS},
            "required": ["text", "source_ids"], "additionalProperties": False}},
    },
    "required": ["tldr", "summary", "decisions", "action_items", "open_questions"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------- grounding

_STOP = set("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has
have having he her here hers herself him himself his how i if in into is it its itself just let lets
me more most my myself no nor not now of off on once only or other our ours ourselves out over own
same she should so some such than that the their theirs them themselves then there these they this
those through to too under until up very was we were what when where which while who whom why will
with would you your yours yourself yourselves yeah okay ok right um uh like really going gonna want
need think know thing things something get got make made one two also well sure yes maybe just
still even much many way we'll we're i'm it's that's don't can't won't need needs going discussed
discuss agreed agree decided decide said says say team meeting will shall should would
""".split())

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")


def salient_words(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall((text or "").lower()):
        w = w.strip("'-")
        if len(w) < 3 or w in _STOP:
            continue
        if w.isdigit() or len(w) >= 4 or any(c.isdigit() for c in w):
            out.add(_stem(w))
    return out


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            break
    return w[:7]


def nearby_lines(lines: Sequence[Any], t: float, window: float = GROUND_WINDOW_S) -> list[Any]:
    return [ln for ln in lines if (ln["end"] or ln["start"]) >= t - window and ln["start"] <= t + window]


def is_grounded(text: str, t: float | None, lines: Sequence[Any], duration: float,
                window: float = GROUND_WINDOW_S) -> bool:
    """t must fall within the meeting and the transcript within ±window must
    share salient words with the claim (≥1 if the claim has ≤3 salient words,
    else ≥2)."""
    if t is None or t < 0 or t > duration + 2:
        return False
    claim = salient_words(text)
    if not claim:
        return False
    near = set()
    for ln in nearby_lines(lines, t, window):
        near |= salient_words(ln["text"] or "")
    shared = len(claim & near)
    return shared >= (1 if len(claim) <= 3 else 2)


# ---------------------------------------------------------------- map

def _frames_block(frames: Sequence[Any]) -> str:
    out = []
    for f in frames:
        vt = (f["visible_text"] or "").replace("\n", " ")
        if len(vt) > FRAME_TEXT_CHARS:
            vt = vt[:FRAME_TEXT_CHARS] + "…"
        facts = ""
        try:
            kf = json.loads(f["key_facts"] or "[]")
            if kf:
                facts = " Facts: " + "; ".join(str(x) for x in kf[:6])
        except (ValueError, TypeError):
            pass
        out.append(f"[{fmt_ts(f['t'])}] {f['kind'] or 'frame'}: {f['title'] or ''} — {f['caption'] or ''}"
                   f"{' Visible text: ' + vt if vt else ''}{facts}")
    return "\n".join(out)


def _speakers_block(speakers: Sequence[Any]) -> str:
    named = [f"{s['label']} = {s['display_name']}" for s in speakers if s["display_name"]]
    return ("Speakers confirmed by the user: " + ", ".join(named)) if named else ""


def build_map_message(idx: int, total: int, window: Sequence[Any], frames: Sequence[Any],
                      speakers_line: str) -> str:
    start, end = window[0]["start"], max((ln["end"] or ln["start"]) for ln in window)
    parts = [f"Section {idx + 1} of {total}, [{fmt_ts(start)}] to [{fmt_ts(end)}]."]
    if speakers_line:
        parts.append(speakers_line)
    if frames:
        parts.append("Frames (what was on screen):\n" + _frames_block(frames))
    parts.append("Transcript:\n" + render_lines(window))
    return "\n\n".join(parts)


def _clean_section(raw: dict, lo: float, hi: float) -> dict:
    def ts(v: Any) -> float | None:
        return parse_ts(v)

    def s(v: Any, n: int = 600) -> str:
        return (v or "").strip()[:n] if isinstance(v, str) else ""

    return {
        "title": s(raw.get("title"), 120),
        "summary": s(raw.get("summary"), 2000),
        "decisions": [{"text": s(d.get("text")), "t": ts(d.get("t"))}
                      for d in raw.get("decisions", []) if s(d.get("text"))],
        "action_items": [{"owner": _owner(a.get("owner")), "task": s(a.get("task")),
                          "due": s(a.get("due"), 80) or None, "t": ts(a.get("t"))}
                         for a in raw.get("action_items", []) if s(a.get("task"))],
        "open_questions": [{"text": s(q.get("text")), "t": ts(q.get("t"))}
                           for q in raw.get("open_questions", []) if s(q.get("text"))],
        "speaker_names": [{"label": s(n.get("label"), 8).upper(), "name": s(n.get("name"), 80),
                           "evidence_t": ts(n.get("evidence_t"))}
                          for n in raw.get("speaker_names", []) if s(n.get("name"))],
        "start": lo, "end": hi,
    }


_NULLISH = {"", "null", "none", "n/a", "na", "unknown", "unclear", "unassigned", "-", "tbd"}


def _owner(v: Any) -> str | None:
    if not isinstance(v, str) or v.strip().lower() in _NULLISH:
        return None
    v = v.strip()[:80]
    m = re.fullmatch(r"(?i)s(?:peaker)?\s*(\d)", v)
    return f"S{m.group(1)}" if m else v


# ---------------------------------------------------------------- reduce

def build_reduce_input(sections: Sequence[dict], max_tokens: int = REDUCE_MAX_TOKENS
                       ) -> tuple[str, dict[str, tuple[str, dict]]]:
    """Render sections with item ids. Returns (text, id -> (kind, item)).
    Section summaries are truncated if the whole input would exceed max_tokens."""
    catalog: dict[str, tuple[str, dict]] = {}
    counters = {"decisions": 0, "action_items": 0, "open_questions": 0}
    prefix = {"decisions": "D", "action_items": "A", "open_questions": "Q"}
    blocks_items: list[list[str]] = []
    for sec in sections:
        items = []
        for kind in ("decisions", "action_items", "open_questions"):
            for it in sec.get(kind, []):
                counters[kind] += 1
                iid = f"{prefix[kind]}{counters[kind]}"
                catalog[iid] = (kind, it)
                if kind == "action_items":
                    extra = f" (owner: {it['owner'] or 'unassigned'}{', due: ' + it['due'] if it['due'] else ''})"
                    items.append(f"[{iid}] action: {it['task']}{extra}")
                else:
                    items.append(f"[{iid}] {kind[:-1].replace('_', ' ')}: {it['text']}")
        blocks_items.append(items)
    fixed = sum(estimate_tokens("\n".join(b)) for b in blocks_items) + 50 * len(sections)
    budget_chars = max(200, int(((max_tokens - fixed) * 3.5) / max(1, len(sections))))
    out = []
    for sec, items in zip(sections, blocks_items):
        summ = sec.get("summary") or ""
        if len(summ) > budget_chars:
            summ = summ[:budget_chars] + "…"
        head = f"## [{fmt_ts(sec['start'])}–{fmt_ts(sec['end'])}] {sec.get('title') or 'Section'}"
        out.append("\n".join([head, summ] + items))
    return "\n\n".join(out), catalog


def merge_reduced(reduced: dict, catalog: dict[str, tuple[str, dict]]) -> dict[str, list[dict]]:
    """Resolve reduce output items to their source items; t = earliest source
    t. Items without any valid source id are dropped. Source items the reduce
    step forgot entirely are NOT re-added (the model decided they were trivial
    or duplicates)."""
    out: dict[str, list[dict]] = {"decisions": [], "action_items": [], "open_questions": []}
    for kind in out:
        for it in reduced.get(kind, []) or []:
            srcs = [catalog[i][1] for i in _norm_ids(it.get("source_ids")) if i in catalog and catalog[i][0] == kind]
            if not srcs:
                continue
            ts = [s["t"] for s in srcs if s.get("t") is not None]
            t = min(ts) if ts else None
            if kind == "action_items":
                owner = _owner(it.get("owner")) or next((s["owner"] for s in srcs if s["owner"]), None)
                due = (it.get("due") or "").strip() or next((s["due"] for s in srcs if s["due"]), None)
                if due and due.lower() in _NULLISH:
                    due = None
                out[kind].append({"owner": owner, "task": (it.get("task") or srcs[0]["task"]).strip(),
                                  "due": due, "t": t, "_src": srcs})
            else:
                out[kind].append({"text": (it.get("text") or srcs[0]["text"]).strip(), "t": t, "_src": srcs})
    return out


def _norm_ids(ids: Any) -> list[str]:
    if not isinstance(ids, list):
        return []
    return [str(i).strip().strip("[]").upper() for i in ids]


def fallback_merge(sections: Sequence[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {"decisions": [], "action_items": [], "open_questions": []}
    for sec in sections:
        for kind in out:
            for it in sec.get(kind, []):
                out[kind].append({**it, "_src": [it]})
    return out


# ---------------------------------------------------------------- assemble

def ground_items(merged: dict[str, list[dict]], lines: Sequence[Any], duration: float,
                 default_t: float = 0.0) -> dict[str, list[dict]]:
    """Attach `grounded`: an item is grounded if its own text, or any source
    item's text, overlaps the transcript near its t (or near a source's t)."""
    out: dict[str, list[dict]] = {}
    for kind, items in merged.items():
        res = []
        for it in items:
            text_key = "task" if kind == "action_items" else "text"
            cands = [(it[text_key], it.get("t"))] + [(s[text_key], s.get("t")) for s in it.get("_src", [])]
            grounded = any(is_grounded(txt, t, lines, duration) for txt, t in cands)
            clean = {k: v for k, v in it.items() if k != "_src"}
            if clean.get("t") is None:
                clean["t"] = default_t
                grounded = False
            clean["t"] = round(float(clean["t"]), 2)
            clean["grounded"] = bool(grounded)
            res.append(clean)
        res.sort(key=lambda x: x["t"])
        out[kind] = res
    return out


def speaker_suggestions(sections: Sequence[dict], lines: Sequence[Any], labels: set[str]) -> list[dict]:
    """Pick one name per label (most frequent across sections, earliest
    evidence). The name must actually appear in the transcript within ±90 s
    of the evidence timestamp, else it is discarded."""
    votes: dict[str, dict[str, list[float]]] = {}
    for sec in sections:
        for n in sec.get("speaker_names", []):
            label, name, t = n["label"], n["name"].strip(), n.get("evidence_t")
            if label not in labels or not name or t is None:
                continue
            first = name.split()[0].lower()
            if len(first) < 2 or not any(first in (ln["text"] or "").lower()
                                         for ln in nearby_lines(lines, t)):
                continue
            votes.setdefault(label, {}).setdefault(name, []).append(t)
    out = []
    for label, names in sorted(votes.items()):
        name, ts = max(names.items(), key=lambda kv: (len(kv[1]), -min(kv[1])))
        out.append({"label": label, "name": name, "evidence_t": round(min(ts), 2)})
    return out


def key_visuals(frames: Sequence[Any], limit: int = MAX_KEY_VISUALS) -> list[dict]:
    good = [f for f in frames if (f["relevance"] or 0) >= 2]
    good.sort(key=lambda f: (-(f["relevance"] or 0), f["t"]))
    good = sorted(good[:limit], key=lambda f: f["t"])
    return [{"frame_id": f["id"], "t": round(float(f["t"]), 2),
             "caption": f["caption"] or f["title"] or ""} for f in good]


def empty_notes(message: str) -> dict:
    return {"tldr": [], "summary": message, "chapters": [], "decisions": [], "action_items": [],
            "open_questions": [], "key_visuals": [], "speaker_suggestions": []}


def synthesize(client: LLMClient, lines: Sequence[Any], frames: Sequence[Any], speakers: Sequence[Any],
               duration: float, *, concurrency: int = 2, cache_dir=None,
               progress=None, should_stop=None) -> dict:
    """Pure-ish core (no DB): returns the Notes JSON dict."""
    lines = [ln for ln in lines if (ln["text"] or "").strip()]
    if not lines:
        return empty_notes("No transcript was available for this meeting.")
    duration = max(duration or 0.0, max((ln["end"] or ln["start"]) for ln in lines))
    windows = split_windows(lines, MAP_WINDOW_S, MAP_MAX_TOKENS)
    bounds = []
    for i, w in enumerate(windows):
        lo = w[0]["start"]
        hi = windows[i + 1][0]["start"] if i + 1 < len(windows) else max((ln["end"] or ln["start"]) for ln in w)
        bounds.append((lo, hi))
    speakers_line = _speakers_block(speakers)
    system = load_prompt("synthesis_map_system.txt")
    sections: list[dict | None] = [None] * len(windows)

    def one(i: int) -> dict:
        lo, hi = bounds[i]
        win_frames = [f for f in frames if lo <= f["t"] < hi or (i == len(windows) - 1 and f["t"] >= lo)]
        msg = build_map_message(i, len(windows), windows[i], win_frames, speakers_line)
        key = hashlib.sha256(f"{PROMPT_VERSION}\n{client.model}\n{system}\n{msg}".encode()).hexdigest()[:32]
        cached = cache_dir / f"map_{key}.json" if cache_dir else None
        if cached is not None and cached.exists():
            try:
                return _clean_section(json.loads(cached.read_text()), lo, hi)
            except (ValueError, OSError):
                pass
        raw = client.chat_json([{"role": "system", "content": system}, {"role": "user", "content": msg}],
                               MAP_SCHEMA, name="section_notes", max_tokens=4096)
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(raw))
        return _clean_section(raw, lo, hi)

    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futs = [pool.submit(one, i) for i in range(len(windows))]
        for i, fut in enumerate(futs):
            try:
                sections[i] = fut.result()
            except LLMError:
                failed += 1
                lo, hi = bounds[i]
                sections[i] = {"title": "", "summary": "", "decisions": [], "action_items": [],
                               "open_questions": [], "speaker_names": [], "start": lo, "end": hi}
            if progress:
                progress(0.85 * (i + 1) / len(windows), f"section {i + 1}/{len(windows)}")
            if should_stop and should_stop():
                for f in futs:
                    f.cancel()
                raise _Stopped()
    if failed > len(windows) / 2:
        raise LLMError(f"{failed} of {len(windows)} sections failed")
    secs: list[dict] = [s for s in sections if s is not None]

    chapters = [{"title": s["title"] or f"Part {i + 1}", "start": round(s["start"], 2),
                 "end": round(s["end"], 2), "summary": s["summary"]} for i, s in enumerate(secs)]

    if progress:
        progress(0.87, "merging sections")
    reduce_text, catalog = build_reduce_input(secs)
    tldr: list[str] = []
    summary = ""
    try:
        reduced = client.chat_json(
            [{"role": "system", "content": load_prompt("synthesis_reduce_system.txt")},
             {"role": "user", "content": "Section notes, in time order:\n\n" + reduce_text}],
            REDUCE_SCHEMA, name="meeting_notes", max_tokens=8192)
        merged = merge_reduced(reduced, catalog)
        # guard: if the reduce step lost (nearly) everything, keep the raw items
        if catalog and sum(len(v) for v in merged.values()) == 0:
            merged = fallback_merge(secs)
        tldr = [str(x).strip() for x in reduced.get("tldr", []) if str(x).strip()][:6]
        summary = (reduced.get("summary") or "").strip()
    except LLMError:
        merged = fallback_merge(secs)
    if not summary:
        summary = "\n\n".join(s["summary"] for s in secs if s["summary"])
    if not tldr:
        tldr = [c["title"] for c in chapters if c["title"]][:5]

    grounded = ground_items(merged, lines, duration, default_t=0.0)
    labels = {s["label"] for s in speakers} | {ln["speaker"] for ln in lines if ln["speaker"]}
    return {
        "tldr": tldr,
        "summary": summary,
        "chapters": chapters,
        "decisions": grounded["decisions"],
        "action_items": grounded["action_items"],
        "open_questions": grounded["open_questions"],
        "key_visuals": key_visuals(frames),
        "speaker_suggestions": speaker_suggestions(secs, lines, labels),
    }


class _Stopped(Exception):
    pass


# ---------------------------------------------------------------- stage

def run(ctx: Any) -> None:
    from quill.stages import StageFailed  # lazy: owned by A

    conn = ctx.db()
    try:
        meeting = conn.execute("SELECT * FROM meetings WHERE id=?", (ctx.meeting_id,)).fetchone()
        lines = conn.execute("SELECT id, speaker, start, end, text FROM transcript_lines "
                             "WHERE meeting_id=? ORDER BY start, id", (ctx.meeting_id,)).fetchall()
        speakers = conn.execute("SELECT * FROM speakers WHERE meeting_id=? ORDER BY label",
                                (ctx.meeting_id,)).fetchall()
        frames = []
        if ctx.mode == "video":
            frames = conn.execute("SELECT * FROM frames WHERE meeting_id=? AND kind IS NOT NULL "
                                  "AND relevance >= 1 ORDER BY t", (ctx.meeting_id,)).fetchall()
    finally:
        conn.close()
    if meeting is None:
        raise StageFailed("meeting not found")

    client = LLMClient.from_settings(ctx.settings, "TEXT_MODEL", timeout=180.0)
    if any((ln["text"] or "").strip() for ln in lines):
        client.check_model_enabled(force=True)
    try:
        notes = synthesize(client, lines, frames, speakers, float(meeting["duration_s"] or 0),
                           concurrency=max(1, int(setting(ctx.settings, "VISION_CONCURRENCY", 2))),
                           cache_dir=ctx.media_dir / "synthesis",
                           progress=ctx.progress, should_stop=ctx.should_stop)
    except _Stopped:
        from quill.pipeline.moments import cancelled
        raise cancelled() from None
    except LLMError as e:
        raise StageFailed(f"synthesis failed: {e}") from None

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = ctx.db()
    try:
        with conn:
            row = conn.execute("SELECT version FROM notes WHERE meeting_id=?", (ctx.meeting_id,)).fetchone()
            version = (row["version"] if row else 0) + 1
            conn.execute("INSERT OR REPLACE INTO notes(meeting_id, version, json, model, created_at) "
                         "VALUES (?,?,?,?,?)",
                         (ctx.meeting_id, version, json.dumps(notes, ensure_ascii=False), client.model, now))
            conn.execute("UPDATE speakers SET suggested_name=NULL, suggestion_evidence_t=NULL WHERE meeting_id=?",
                         (ctx.meeting_id,))
            for s in notes["speaker_suggestions"]:
                conn.execute("UPDATE speakers SET suggested_name=?, suggestion_evidence_t=? "
                             "WHERE meeting_id=? AND label=?", (s["name"], s["evidence_t"], ctx.meeting_id, s["label"]))
    finally:
        conn.close()
    n = notes
    ctx.log(f"synthesize: v{version}, {len(n['chapters'])} chapters, {len(n['decisions'])} decisions, "
            f"{len(n['action_items'])} actions, {sum(not x['grounded'] for k in ('decisions', 'action_items', 'open_questions') for x in n[k])} ungrounded")
    ctx.progress(1.0, "notes ready")
