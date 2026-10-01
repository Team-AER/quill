"""Frame probabilities -> speaker turns. Pure functions, numpy only.

Pipeline (docs/PLAN.md section 4.3):
  probs [T, K] --threshold--> active [T, K] bool --median filter-->
  per-speaker segments --drop short--> --merge small gaps-->
  (optional cap to max_speakers) --> overlap marking --> relabel S1..Sn
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

FRAME_S = 0.01
MODEL_SLOTS = 8


@dataclass(frozen=True)
class PostParams:
    threshold: float = 0.5
    median_frames: int = 11          # odd window, in frames (110 ms at 10 ms/frame)
    min_segment_s: float = 0.3       # drop segments shorter than this
    merge_gap_s: float = 0.8         # merge same-speaker gaps shorter than this
    min_overlap_s: float = 0.2       # a turn is "overlap" if it shares >= this much time with another speaker
    frame_s: float = FRAME_S


@dataclass
class Turn:
    speaker: str
    start: float
    end: float
    overlap: bool = False

    def as_dict(self) -> dict:
        return {"speaker": self.speaker, "start": round(self.start, 3),
                "end": round(self.end, 3), "overlap": self.overlap}


@dataclass
class Diarization:
    speakers: list[str]
    turns: list[Turn]
    overlaps: list[dict] = field(default_factory=list)   # [{start, end, speakers}]
    warnings: list[str] = field(default_factory=list)
    slots_used: int = 0


# ------------------------------------------------------------------ frames

def binarize(probs: np.ndarray, threshold: float = 0.5) -> np.ndarray:
    """[T, K] probabilities -> [T, K] bool, strictly above threshold."""
    probs = np.asarray(probs)
    if probs.ndim != 2:
        raise ValueError(f"expected [T, K] probabilities, got shape {probs.shape}")
    return probs > threshold


def median_filter(active: np.ndarray, window: int) -> np.ndarray:
    """Median filter along time for a boolean [T, K] matrix.

    For binary data the median is a majority vote, computed with a cumulative
    sum in O(T*K). Edges are padded by repeating the edge value (like
    scipy.ndimage.median_filter mode='nearest')."""
    active = np.asarray(active, dtype=bool)
    if window <= 1 or active.shape[0] == 0:
        return active.copy()
    if window % 2 == 0:
        raise ValueError("median window must be odd")
    half = window // 2
    padded = np.concatenate(
        [np.repeat(active[:1], half, axis=0), active, np.repeat(active[-1:], half, axis=0)]
    ).astype(np.int32)
    csum = np.concatenate([np.zeros((1, active.shape[1]), np.int32), np.cumsum(padded, axis=0)])
    counts = csum[window:] - csum[:-window]
    return counts > half


def runs(mask_1d: np.ndarray) -> list[tuple[int, int]]:
    """Half-open [start, end) frame runs where mask is True."""
    m = np.asarray(mask_1d, dtype=bool)
    if m.size == 0:
        return []
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return list(zip(starts.tolist(), ends.tolist()))


# ------------------------------------------------------------------ segments

Segment = tuple[float, float]


def drop_short(segs: list[Segment], min_len: float) -> list[Segment]:
    return [(s, e) for s, e in segs if e - s >= min_len - 1e-9]


def merge_gaps(segs: list[Segment], max_gap: float) -> list[Segment]:
    """Merge consecutive segments of one speaker whose gap is < max_gap."""
    out: list[Segment] = []
    for s, e in sorted(segs):
        if out and s - out[-1][1] < max_gap - 1e-9:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def cap_speakers(active: np.ndarray, probs: np.ndarray, max_speakers: int) -> np.ndarray:
    """Keep at most `max_speakers` channels (those with the most active frames).

    Each dropped channel's active frames are folded into the kept channel with
    the highest mean probability over those frames, i.e. an extra slot is
    assumed to be a split of a real speaker rather than a new person."""
    active = np.asarray(active, dtype=bool)
    totals = active.sum(axis=0)
    used = np.flatnonzero(totals > 0)
    if max_speakers <= 0 or len(used) <= max_speakers:
        return active.copy()
    keep = sorted(used[np.argsort(-totals[used], kind="stable")][:max_speakers].tolist())
    out = active.copy()
    for ch in used:
        if ch in keep:
            continue
        frames = active[:, ch]
        target = keep[int(np.argmax(probs[frames][:, keep].mean(axis=0)))]
        out[:, target] |= frames
        out[:, ch] = False
    return out


def overlap_regions(per_speaker: dict[int, list[Segment]], min_len: float = 0.0) -> list[dict]:
    """Time regions where two or more speakers are active: [{start, end, speakers}]."""
    events: list[tuple[float, int, int]] = []
    for spk, segs in per_speaker.items():
        for s, e in segs:
            events.append((s, 1, spk))
            events.append((e, -1, spk))
    # ends before starts at the same instant: touching segments do not overlap
    events.sort(key=lambda x: (x[0], x[1]))
    active: set[int] = set()
    regions: list[dict] = []
    cur_start: float | None = None
    cur_spk: set[int] = set()
    for t, kind, spk in events:
        if kind == 1:
            active.add(spk)
        else:
            active.discard(spk)
        if len(active) >= 2 and cur_start is None:
            cur_start, cur_spk = t, set(active)
        elif len(active) >= 2:
            cur_spk |= active
        elif len(active) < 2 and cur_start is not None:
            if t - cur_start >= min_len - 1e-9 and t > cur_start:
                regions.append({"start": cur_start, "end": t, "speakers": sorted(cur_spk)})
            cur_start, cur_spk = None, set()
    return regions


def _overlap_with(seg: Segment, others: list[Segment]) -> float:
    s, e = seg
    return sum(max(0.0, min(e, oe) - max(s, os_)) for os_, oe in others)


# ------------------------------------------------------------------ driver

def postprocess(
    probs: np.ndarray,
    params: PostParams = PostParams(),
    max_speakers: int | None = None,
) -> Diarization:
    probs = np.asarray(probs, dtype=np.float32)
    warnings: list[str] = []
    active = median_filter(binarize(probs, params.threshold), params.median_frames)

    fs = params.frame_s
    per_ch: dict[int, list[Segment]] = {}
    for ch in range(active.shape[1]):
        segs = [(a * fs, b * fs) for a, b in runs(active[:, ch])]
        segs = merge_gaps(drop_short(segs, params.min_segment_s), params.merge_gap_s)
        if segs:
            per_ch[ch] = segs

    slots_used = len(per_ch)
    if probs.shape[1] >= MODEL_SLOTS and slots_used >= MODEL_SLOTS:
        warnings.append(
            f"all {MODEL_SLOTS} speaker slots of the model are in use; meetings with more than "
            f"{MODEL_SLOTS} speakers will have similar voices merged"
        )

    if max_speakers and max_speakers > 0 and slots_used > max_speakers:
        # re-run segment building on the capped frame matrix (only surviving channels' frames)
        mask = np.zeros_like(active)
        for ch, segs in per_ch.items():
            for s, e in segs:
                mask[int(round(s / fs)): int(round(e / fs)), ch] = True
        capped = cap_speakers(mask, probs, max_speakers)
        per_ch = {}
        for ch in range(capped.shape[1]):
            segs = merge_gaps([(a * fs, b * fs) for a, b in runs(capped[:, ch])], params.merge_gap_s)
            if segs:
                per_ch[ch] = segs
        warnings.append(
            f"model found {slots_used} speakers; merged down to max_speakers={max_speakers}"
        )

    # relabel by first appearance
    order = sorted(per_ch, key=lambda ch: (per_ch[ch][0][0], ch))
    label = {ch: f"S{i + 1}" for i, ch in enumerate(order)}

    turns: list[Turn] = []
    for ch, segs in per_ch.items():
        others = [seg for och, osegs in per_ch.items() if och != ch for seg in osegs]
        for seg in segs:
            ov = _overlap_with(seg, others) >= params.min_overlap_s - 1e-9
            turns.append(Turn(label[ch], seg[0], seg[1], ov))
    turns.sort(key=lambda t: (t.start, int(t.speaker[1:])))

    regions = overlap_regions(per_ch, params.min_overlap_s)
    for r in regions:
        r["speakers"] = sorted((label[c] for c in r["speakers"]), key=lambda s: int(s[1:]))
        r["start"], r["end"] = round(r["start"], 3), round(r["end"], 3)

    return Diarization(
        speakers=[label[ch] for ch in order],
        turns=turns,
        overlaps=regions,
        warnings=warnings,
        slots_used=slots_used,
    )
