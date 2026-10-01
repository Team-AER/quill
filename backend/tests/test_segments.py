"""turns -> transcription segments (PLAN §4.4)."""

from __future__ import annotations

import pytest

from quill.pipeline.segments import (
    Turn,
    absorb_micro,
    build_segments,
    merge_same_speaker,
    split_long,
    to_turns,
)


def T(s, a, b, o=False):
    return Turn(s, a, b, o)


def spans(segs):
    return [(s.speaker, s.start, s.end) for s in segs]


def test_empty():
    assert build_segments([], 100.0) == []


def test_single_turn_padded_and_clamped():
    segs = build_segments([T("S1", 0.1, 5.0)], duration=5.1)
    assert spans(segs) == [("S1", 0.0, 5.1)]
    assert segs[0].speech_start == 0.1 and segs[0].speech_end == 5.0
    assert segs[0].idx == 0


def test_padding_without_duration():
    segs = build_segments([T("S1", 2.0, 5.0)])
    assert spans(segs) == [("S1", 1.75, 5.25)]


def test_merge_consecutive_same_speaker():
    segs = build_segments([T("S1", 0, 3), T("S1", 3.5, 6), T("S2", 6.2, 9), T("S1", 9.5, 12)], 20)
    assert [s.speaker for s in segs] == ["S1", "S2", "S1"]
    assert segs[0].speech_start == 0 and segs[0].speech_end == 6
    assert segs[0].turns == [(0, 3), (3.5, 6)]
    assert [s.idx for s in segs] == [0, 1, 2]


def test_unsorted_input_and_zero_length_turns_dropped():
    segs = build_segments([T("S2", 5, 8), T("S1", 0, 4), T("S1", 4.5, 4.5)], 10)
    assert [s.speaker for s in segs] == ["S1", "S2"]


def test_large_silence_gap_not_merged():
    segs = build_segments([T("S1", 0, 5), T("S1", 100, 105)], 200)
    assert len(segs) == 2


def test_micro_interjection_absorbed_and_neighbours_merged():
    # S1 talks, S2 says "yeah" (0.5 s) in a gap, S1 continues.
    segs = build_segments([T("S1", 0, 10), T("S2", 10.2, 10.7), T("S1", 11, 20)], 30)
    assert len(segs) == 1
    s = segs[0]
    assert s.speaker == "S1"
    assert (s.speech_start, s.speech_end) == (0, 20)
    assert s.interjections == [{"speaker": "S2", "start": 10.2, "end": 10.7}]


def test_micro_inside_other_turn_is_interjection():
    segs = build_segments([T("S1", 0, 10, True), T("S2", 4, 4.6, True)], 10)
    assert len(segs) == 1
    assert segs[0].interjections[0]["speaker"] == "S2"
    assert segs[0].overlap is True


def test_micro_at_edge_extends_host():
    # a trailing micro-turn right after the host: host span covers it
    segs = build_segments([T("S1", 0, 10), T("S2", 10.3, 11.0)], 30)
    assert len(segs) == 1
    assert segs[0].speech_end == 11.0
    assert segs[0].end == 11.25


def test_isolated_micro_kept_as_own_segment():
    # "Yes." answered 20 s after anything else: must not be dropped.
    segs = build_segments([T("S1", 0, 10), T("S2", 30, 30.8)], 60)
    assert [s.speaker for s in segs] == ["S1", "S2"]
    assert segs[1].interjections == []


def test_all_micro_kept():
    segs = build_segments([T("S1", 0, 0.5), T("S2", 0.6, 1.0), T("S1", 1.1, 1.5)], 2)
    assert [s.speaker for s in segs] == ["S1", "S2", "S1"]


def test_micro_same_speaker_as_only_neighbour_is_folded_in():
    # S1 micro-turn 29 s... gap bigger than max_merge_gap is kept apart, but a
    # same-speaker micro right next to the host is merged in step 2 anyway.
    segs = build_segments([T("S1", 0, 10), T("S1", 10.5, 11)], 20)
    assert len(segs) == 1 and segs[0].speech_end == 11


def test_micro_prefers_containing_host_then_nearest():
    turns = [T("S1", 0, 10), T("S3", 9.8, 10.3), T("S2", 10.5, 20)]
    segs = build_segments(turns, 30)
    s1 = next(s for s in segs if s.speaker == "S1")
    assert s1.interjections and s1.interjections[0]["speaker"] == "S3"


def test_long_segment_split_at_largest_internal_gap():
    turns = [T("S1", 0, 400), T("S1", 405, 700), T("S1", 700.5, 900)]
    segs = build_segments(turns, 1000)
    assert len(segs) == 2
    a, b = segs
    # cut in the middle of the 5 s gap (400..405)
    assert a.speech_end == pytest.approx(402.5)
    assert b.speech_start == pytest.approx(402.5)
    # outer edges padded, internal cut not padded
    assert a.start == 0 and a.end == pytest.approx(402.5)
    assert b.start == pytest.approx(402.5) and b.end == pytest.approx(900.25)
    assert all(s.speech_end - s.speech_start <= 600 for s in segs)


def test_long_segment_without_gap_split_at_midpoint_recursively():
    segs = build_segments([T("S1", 0, 1500)], 1500)
    assert len(segs) == 4  # 1500 -> 750 -> 375 x 4
    assert all(s.speech_end - s.speech_start <= 600 for s in segs)
    assert segs[0].start == 0 and segs[-1].end == 1500
    for x, y in zip(segs, segs[1:]):
        assert x.end == y.start  # contiguous, no duplicated audio
    assert [s.idx for s in segs] == [0, 1, 2, 3]


def test_split_keeps_interjections_on_the_right_side():
    turns = [T("S1", 0, 500), T("S2", 100, 100.5), T("S1", 510, 1000), T("S2", 800, 800.4)]
    segs = build_segments(turns, 1000)
    assert len(segs) == 2
    assert [i["start"] for i in segs[0].interjections] == [100]
    assert [i["start"] for i in segs[1].interjections] == [800]


def test_segment_exactly_at_max_not_split():
    assert len(build_segments([T("S1", 0, 600)], 700)) == 1


def test_turns_clamped_to_duration():
    segs = build_segments([T("S1", -1, 5), T("S2", 8, 12)], 10)
    assert segs[0].speech_start == 0
    assert segs[-1].speech_end == 10 and segs[-1].end == 10


def test_to_turns_accepts_dicts_rows_and_tuples():
    rows = [
        {"speaker": "S1", "start": 0, "end": 1, "overlap": 1},
        ("S2", 1, 2),
        ("S3", 2, 3, True),
        T("S4", 3, 4),
    ]
    ts = to_turns(rows)
    assert [t.speaker for t in ts] == ["S1", "S2", "S3", "S4"]
    assert ts[0].overlap is True and ts[1].overlap is False


def test_to_turns_accepts_sqlite_rows():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE turns(speaker, start, end, overlap)")
    conn.execute("INSERT INTO turns VALUES ('S1', 0, 2, 0)")
    segs = build_segments(conn.execute("SELECT * FROM turns").fetchall(), 2)
    assert spans(segs) == [("S1", 0, 2)]


def test_building_blocks_directly():
    merged = merge_same_speaker([T("S1", 0, 2), T("S1", 2.5, 4)])
    assert len(merged) == 1 and merged[0].turns == [(0, 2), (2.5, 4)]
    absorbed = absorb_micro(merge_same_speaker([T("S1", 0, 5), T("S2", 5.1, 5.5)]))
    assert len(absorbed) == 1
    pieces = split_long(merge_same_speaker([T("S1", 0, 100)]), max_s=30)
    assert len(pieces) == 4
    assert pieces[0][1] is False and pieces[0][2] is True  # (seg, cut_start, cut_end)


def test_realistic_meeting_properties():
    import random

    rnd = random.Random(7)
    turns, t = [], 0.0
    speakers = ["S1", "S2", "S3"]
    while t < 3600:
        d = rnd.choice([0.4, 0.8, 1.0, 3, 8, 20, 45, 90])
        turns.append(T(rnd.choice(speakers), t, t + d))
        t += d + rnd.choice([0.0, 0.1, 0.5, 2.0])
    segs = build_segments(turns, 3600)
    assert segs
    for s in segs:
        assert 0 <= s.start <= s.speech_start < s.speech_end <= s.end <= 3600
        assert s.speech_end - s.speech_start <= 600 + 1e-6
    # micro-turns rarely survive as their own STT call
    micro = [s for s in segs if s.speech_end - s.speech_start < 1.2]
    assert len(micro) <= len(segs) * 0.1
    # every original turn's midpoint is covered by some segment's clip
    for tr in turns:
        mid = (tr.start + tr.end) / 2
        if mid <= 3600:
            assert any(s.start <= mid <= s.end for s in segs), tr
