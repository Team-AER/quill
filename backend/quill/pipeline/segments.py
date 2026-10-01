"""Diarization turns -> transcription segments (PLAN §4.4 step 1).

Pure functions, no I/O. The transcribe stage cuts one clip per segment and sends
it to aer-stt-v1, so a segment is "one speaker talking", with short
interjections from other speakers absorbed as markers instead of becoming their
own STT calls.

Algorithm
---------
1. Drop empty turns, sort by start.
2. Merge consecutive same-speaker turns (no other speaker's turn in between)
   unless the silence between them exceeds ``max_merge_gap``.
3. Absorb micro-segments (< ``micro_s``) into an adjacent / surrounding segment
   of another speaker as an interjection marker. A micro-segment that is isolated
   (nothing within ``absorb_gap``) stays a segment of its own so its words are not
   lost; if every segment is micro, nothing is absorbed. The host's span is
   extended to cover the interjection, so the clip still contains those words.
   After absorption, neighbours of the same speaker are merged again
   (S1 long, S2 "yeah", S1 long -> one S1 segment with an interjection).
4. Split segments longer than ``max_s`` at the largest internal gap between
   that speaker's own turns; midpoint when there is no gap. Recursive.
5. Pad each segment by ``pad_s`` on both sides, clamped to [0, duration].
   Internal split points are not padded (they already sit in silence, and
   padding a midpoint cut would duplicate words in both clips).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

MICRO_S = 1.2
PAD_S = 0.25
MAX_S = 600.0
MAX_MERGE_GAP = 30.0
ABSORB_GAP = 1.0
EPS = 1e-6


@dataclass(frozen=True)
class Turn:
    speaker: str
    start: float
    end: float
    overlap: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Segment:
    """A transcription segment. ``start``/``end`` are the padded clip bounds;
    ``speech_start``/``speech_end`` the unpadded speech span used for transcript
    lines."""

    speaker: str
    start: float
    end: float
    speech_start: float
    speech_end: float
    turns: list[tuple[float, float]] = field(default_factory=list)  # own turns
    interjections: list[dict[str, Any]] = field(default_factory=list)
    overlap: bool = False
    idx: int = 0

    @property
    def duration(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict[str, Any]:
        return {
            "idx": self.idx,
            "speaker": self.speaker,
            "start": self.start,
            "end": self.end,
            "speech_start": self.speech_start,
            "speech_end": self.speech_end,
            "interjections": list(self.interjections),
            "overlap": self.overlap,
        }


def to_turns(rows: Iterable[Any]) -> list[Turn]:
    """Accept Turn objects, mappings / sqlite3.Row with speaker,start,end[,overlap],
    or (speaker, start, end[, overlap]) tuples."""
    out: list[Turn] = []
    for r in rows:
        if isinstance(r, Turn):
            t = r
        elif isinstance(r, Mapping) or hasattr(r, "keys"):
            keys = r.keys()
            t = Turn(
                str(r["speaker"]),
                float(r["start"]),
                float(r["end"]),
                bool(r["overlap"]) if "overlap" in keys and r["overlap"] is not None else False,
            )
        else:
            seq = tuple(r)
            t = Turn(str(seq[0]), float(seq[1]), float(seq[2]), bool(seq[3]) if len(seq) > 3 else False)
        out.append(t)
    return out


# ---------------------------------------------------------------- building blocks


@dataclass
class _Seg:
    speaker: str
    start: float
    end: float
    turns: list[tuple[float, float]]
    interjections: list[dict[str, Any]]
    overlap: bool

    @property
    def duration(self) -> float:
        return self.end - self.start


def merge_same_speaker(turns: list[Turn], max_gap: float = MAX_MERGE_GAP) -> list[_Seg]:
    """Merge consecutive (in start order) same-speaker turns."""
    segs: list[_Seg] = []
    for t in sorted(turns, key=lambda t: (t.start, t.end)):
        if t.end - t.start <= EPS:
            continue
        last = segs[-1] if segs else None
        if last is not None and last.speaker == t.speaker and t.start - last.end <= max_gap:
            last.end = max(last.end, t.end)
            last.turns.append((t.start, t.end))
            last.overlap = last.overlap or t.overlap
        else:
            segs.append(_Seg(t.speaker, t.start, t.end, [(t.start, t.end)], [], t.overlap))
    return segs


def _gap(a: _Seg, b: _Seg) -> float:
    """Distance between two spans (0 when they touch or overlap)."""
    return max(0.0, max(a.start, b.start) - min(a.end, b.end))


def absorb_micro(
    segs: list[_Seg],
    micro_s: float = MICRO_S,
    absorb_gap: float = ABSORB_GAP,
    max_merge_gap: float = MAX_MERGE_GAP,
) -> list[_Seg]:
    """Turn micro-segments into interjections of a neighbouring segment."""
    if not segs or all(s.duration < micro_s for s in segs):
        return list(segs)
    hosts = [s for s in segs if s.duration >= micro_s]
    kept_micro: list[_Seg] = []
    for m in (s for s in segs if s.duration < micro_s):
        # Candidates: host segments of another speaker that contain or are within
        # absorb_gap of the micro-turn. Prefer the one that contains it, then the
        # nearest, then the earlier one (the speaker being interrupted).
        best = None
        best_key = None
        for h in hosts:
            if h.speaker == m.speaker:
                continue
            g = _gap(h, m)
            if g > absorb_gap:
                continue
            contains = h.start - EPS <= m.start and m.end <= h.end + EPS
            key = (0 if contains else 1, g, 0 if h.start <= m.start else 1, h.start)
            if best_key is None or key < best_key:
                best, best_key = h, key
        if best is None:
            # Same-speaker host right next to it? (can happen when max_merge_gap
            # split them) -> fold in as own speech.
            for h in hosts:
                if h.speaker == m.speaker and _gap(h, m) <= absorb_gap:
                    h.start, h.end = min(h.start, m.start), max(h.end, m.end)
                    h.turns.extend(m.turns)
                    h.turns.sort()
                    h.interjections.extend(m.interjections)
                    h.overlap = h.overlap or m.overlap
                    best = h
                    break
            if best is None:
                kept_micro.append(m)
            continue
        best.interjections.append(
            {"speaker": m.speaker, "start": round(m.start, 3), "end": round(m.end, 3)}
        )
        best.interjections.extend(m.interjections)
        best.start, best.end = min(best.start, m.start), max(best.end, m.end)
        best.overlap = best.overlap or m.overlap
    out = sorted(hosts + kept_micro, key=lambda s: (s.start, s.end))
    # Re-merge same-speaker neighbours that are now adjacent.
    merged: list[_Seg] = []
    for s in out:
        last = merged[-1] if merged else None
        if last is not None and last.speaker == s.speaker and s.start - last.end <= max_merge_gap:
            last.end = max(last.end, s.end)
            last.turns = sorted(last.turns + s.turns)
            last.interjections.extend(s.interjections)
            last.overlap = last.overlap or s.overlap
        else:
            merged.append(s)
    for s in merged:
        s.interjections.sort(key=lambda i: (i["start"], i["end"]))
    return merged


def _split_point(seg: _Seg) -> float:
    """Middle of the largest gap between the speaker's own turns, else midpoint."""
    turns = sorted(seg.turns)
    best_gap, best_at = 0.0, None
    reach = turns[0][1] if turns else seg.start
    for s, e in turns[1:]:
        gap = s - reach
        at = (reach + s) / 2
        if gap > best_gap + EPS and seg.start + EPS < at < seg.end - EPS:
            best_gap, best_at = gap, at
        reach = max(reach, e)
    return best_at if best_at is not None else (seg.start + seg.end) / 2


def split_long(segs: list[_Seg], max_s: float = MAX_S) -> list[tuple[_Seg, bool, bool]]:
    """Split segments longer than max_s. Returns (segment, cut_at_start, cut_at_end)
    so padding can skip internal cuts."""
    out: list[tuple[_Seg, bool, bool]] = []
    stack = [(s, False, False) for s in reversed(segs)]
    while stack:
        seg, cs, ce = stack.pop()
        if seg.duration <= max_s + EPS:
            out.append((seg, cs, ce))
            continue
        at = _split_point(seg)
        left = _Seg(seg.speaker, seg.start, at, [], [], seg.overlap)
        right = _Seg(seg.speaker, at, seg.end, [], [], seg.overlap)
        for s, e in seg.turns:
            if e <= at:
                left.turns.append((s, e))
            elif s >= at:
                right.turns.append((s, e))
            else:  # a turn straddling a midpoint cut
                left.turns.append((s, at))
                right.turns.append((at, e))
        for i in seg.interjections:
            (left if i["start"] < at else right).interjections.append(i)
        stack.append((right, True, ce))
        stack.append((left, cs, True))
    return out


def build_segments(
    turns: Iterable[Any],
    duration: float | None = None,
    *,
    micro_s: float = MICRO_S,
    pad_s: float = PAD_S,
    max_s: float = MAX_S,
    max_merge_gap: float = MAX_MERGE_GAP,
    absorb_gap: float = ABSORB_GAP,
) -> list[Segment]:
    """Full pipeline: turns -> padded, indexed transcription segments."""
    ts = to_turns(turns)
    if duration is not None and duration > 0:
        ts = [Turn(t.speaker, max(0.0, t.start), min(duration, t.end), t.overlap) for t in ts]
    segs = merge_same_speaker(ts, max_merge_gap)
    segs = absorb_micro(segs, micro_s, absorb_gap, max_merge_gap)
    pieces = split_long(segs, max_s)
    upper = duration if duration is not None and duration > 0 else None
    out: list[Segment] = []
    for n, (s, cut_start, cut_end) in enumerate(pieces):
        start = s.start if cut_start else max(0.0, s.start - pad_s)
        end = s.end if cut_end else s.end + pad_s
        if upper is not None:
            end = min(upper, end)
        out.append(
            Segment(
                speaker=s.speaker,
                start=round(start, 3),
                end=round(end, 3),
                speech_start=round(s.start, 3),
                speech_end=round(s.end, 3),
                turns=[(round(a, 3), round(b, 3)) for a, b in s.turns],
                interjections=list(s.interjections),
                overlap=s.overlap,
                idx=n,
            )
        )
    return out
