import numpy as np
import pytest

from quill_diarizer.postprocess import (
    PostParams,
    binarize,
    cap_speakers,
    drop_short,
    median_filter,
    merge_gaps,
    overlap_regions,
    postprocess,
    runs,
)

F = 0.01  # frame


def probs_from(spans: dict[int, list[tuple[float, float]]], duration: float, k: int = 8, p: float = 0.9):
    T = int(round(duration / F))
    out = np.full((T, k), 0.05, dtype=np.float32)
    for ch, segs in spans.items():
        for s, e in segs:
            out[int(round(s / F)): int(round(e / F)), ch] = p
    return out


# ---- primitives

def test_binarize_strict_threshold():
    a = binarize(np.array([[0.49, 0.5, 0.51]]), 0.5)
    assert a.tolist() == [[False, False, True]]


def test_binarize_rejects_1d():
    with pytest.raises(ValueError):
        binarize(np.zeros(5))


def test_median_filter_removes_spikes_and_fills_holes():
    x = np.zeros((30, 1), bool)
    x[5, 0] = True                 # isolated spike
    x[10:25, 0] = True
    x[17, 0] = False               # 1-frame hole
    y = median_filter(x, 5)[:, 0]
    assert not y[5]
    assert y[17]
    assert y[10:25].all() and not y[:10].any() and not y[25:].any()


def test_median_filter_matches_naive():
    rng = np.random.default_rng(0)
    x = rng.random((200, 3)) > 0.5
    w = 7
    pad = np.concatenate([np.repeat(x[:1], 3, 0), x, np.repeat(x[-1:], 3, 0)])
    naive = np.stack([np.median(pad[i:i + w], axis=0) > 0.5 for i in range(200)])
    assert (median_filter(x, w) == naive).all()


def test_median_filter_even_window_rejected():
    with pytest.raises(ValueError):
        median_filter(np.zeros((5, 1), bool), 4)


def test_median_filter_empty_and_window_one():
    assert median_filter(np.zeros((0, 8), bool), 11).shape == (0, 8)
    x = np.eye(4, dtype=bool)
    assert (median_filter(x, 1) == x).all()


def test_runs():
    assert runs(np.array([0, 1, 1, 0, 1], bool)) == [(1, 3), (4, 5)]
    assert runs(np.array([], bool)) == []


def test_drop_short_and_merge_gaps():
    segs = [(0.0, 0.2), (1.0, 2.0), (2.5, 3.0), (4.0, 5.0)]
    kept = drop_short(segs, 0.3)
    assert kept == [(1.0, 2.0), (2.5, 3.0), (4.0, 5.0)]
    assert merge_gaps(kept, 0.8) == [(1.0, 3.0), (4.0, 5.0)]   # 0.5 gap merges, 1.0 does not


def test_min_segment_boundary_is_inclusive():
    assert drop_short([(0.0, 0.3)], 0.3) == [(0.0, 0.3)]


def test_overlap_regions_touching_is_not_overlap():
    regions = overlap_regions({0: [(0.0, 2.0)], 1: [(2.0, 3.0)]})
    assert regions == []
    regions = overlap_regions({0: [(0.0, 2.0)], 1: [(1.5, 3.0)], 2: [(1.8, 1.9)]})
    assert len(regions) == 1
    assert regions[0]["start"] == 1.5 and regions[0]["end"] == 2.0
    assert regions[0]["speakers"] == [0, 1, 2]


def test_cap_speakers_folds_into_most_similar():
    T = 100
    active = np.zeros((T, 8), bool)
    probs = np.zeros((T, 8), np.float32)
    active[0:50, 0] = True
    active[50:90, 1] = True
    active[90:100, 2] = True           # smallest channel, sounds like speaker 1
    probs[90:100, 1] = 0.4
    probs[90:100, 0] = 0.1
    out = cap_speakers(active, probs, 2)
    assert not out[:, 2].any()
    assert out[90:100, 1].all()
    assert out[0:50, 0].all()


# ---- full pipeline

def test_pipeline_basic_turns_and_relabel_by_first_appearance():
    # model channel 3 speaks first, then channel 0
    probs = probs_from({3: [(0.5, 4.0)], 0: [(4.5, 9.0)]}, 10.0)
    d = postprocess(probs)
    assert d.speakers == ["S1", "S2"]
    assert [(t.speaker, round(t.start, 2), round(t.end, 2)) for t in d.turns] == [
        ("S1", 0.5, 4.0), ("S2", 4.5, 9.0)]
    assert not any(t.overlap for t in d.turns)
    assert d.warnings == []


def test_pipeline_drops_short_and_merges_gaps():
    probs = probs_from({0: [(1.0, 3.0), (3.5, 6.0)], 1: [(7.0, 7.2)]}, 10.0)
    d = postprocess(probs)
    assert d.speakers == ["S1"]                      # 0.2 s blip dropped, speaker never appears
    assert len(d.turns) == 1
    assert (round(d.turns[0].start, 2), round(d.turns[0].end, 2)) == (1.0, 6.0)


def test_pipeline_marks_overlap():
    probs = probs_from({0: [(0.0, 5.0)], 1: [(4.0, 8.0)], 2: [(9.0, 9.5)]}, 10.0)
    d = postprocess(probs)
    flags = {t.speaker: t.overlap for t in d.turns}
    assert flags == {"S1": True, "S2": True, "S3": False}
    assert len(d.overlaps) == 1
    assert d.overlaps[0]["speakers"] == ["S1", "S2"]
    assert d.overlaps[0]["start"] == pytest.approx(4.0, abs=0.02)
    assert d.overlaps[0]["end"] == pytest.approx(5.0, abs=0.02)


def test_tiny_overlap_not_flagged():
    probs = probs_from({0: [(0.0, 5.0)], 1: [(4.9, 8.0)]}, 10.0)   # 0.1 s < min_overlap_s
    d = postprocess(probs)
    assert not any(t.overlap for t in d.turns)


def test_all_slots_warning():
    spans = {ch: [(ch * 2.0, ch * 2.0 + 1.5)] for ch in range(8)}
    d = postprocess(probs_from(spans, 20.0))
    assert d.slots_used == 8
    assert len(d.speakers) == 8
    assert any("all 8 speaker slots" in w for w in d.warnings)


def test_max_speakers_caps():
    spans = {0: [(0.0, 5.0)], 1: [(5.5, 10.0)], 2: [(10.5, 11.5)]}
    d = postprocess(probs_from(spans, 12.0), max_speakers=2)
    assert d.speakers == ["S1", "S2"]
    assert {t.speaker for t in d.turns} == {"S1", "S2"}
    assert any("max_speakers=2" in w for w in d.warnings)


def test_threshold_param():
    probs = probs_from({0: [(0.0, 2.0)]}, 3.0, p=0.6)
    assert postprocess(probs, PostParams(threshold=0.7)).turns == []
    assert len(postprocess(probs, PostParams(threshold=0.5)).turns) == 1


def test_empty_input():
    d = postprocess(np.zeros((0, 8), np.float32))
    assert d.speakers == [] and d.turns == [] and d.overlaps == []


def test_turns_sorted_and_serializable():
    probs = probs_from({1: [(0.0, 1.0), (3.0, 4.0)], 0: [(1.5, 2.5)]}, 5.0)
    d = postprocess(probs)
    starts = [t.start for t in d.turns]
    assert starts == sorted(starts)
    assert set(d.turns[0].as_dict()) == {"speaker", "start", "end", "overlap"}
