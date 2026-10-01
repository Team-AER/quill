"""Phase 0 benchmark: run quill-diarize on AMI inputs, measure wall time, peak RSS and DER.

Usage: python bench/run_bench.py ami10:4,6,8 ami60:4,6,8 ami480:6
Inputs come from bench/prepare_ami.py ($QUILL_BENCH_DIR, default ~/.cache/quill-diarizer-bench).
Appends one JSON line per run to bench/out/results.jsonl.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
BENCH = Path(os.environ.get("QUILL_BENCH_DIR", Path.home() / ".cache" / "quill-diarizer-bench"))
OUT = HERE / "out"
EXE = Path(sys.executable).with_name("quill-diarize")
F = 0.01


def load_rttm(path: Path):
    segs = []
    for line in path.read_text().splitlines():
        p = line.split()
        segs.append((float(p[3]), float(p[3]) + float(p[4]), p[7]))
    return segs


def to_matrix(segs, T):
    labels = sorted({s[2] for s in segs})
    m = np.zeros((T, len(labels)), bool)
    for s, e, lab in segs:
        m[int(round(s / F)): int(round(e / F)), labels.index(lab)] = True
    return m, labels


def der(ref_segs, hyp_segs, duration, collar=0.0):
    """Frame-level DER with optimal 1:1 speaker mapping; collar (s) around reference boundaries."""
    from scipy.optimize import linear_sum_assignment

    T = int(round(duration / F))
    ref, _ = to_matrix(ref_segs, T)
    hyp, _ = to_matrix(hyp_segs, T)
    keep = np.ones(T, bool)
    if collar > 0:
        c = int(round(collar / F))
        for s, e, _l in ref_segs:
            for b in (int(round(s / F)), int(round(e / F))):
                keep[max(0, b - c): b + c] = False
    ref, hyp = ref[keep], hyp[keep]
    nref, nhyp = ref.sum(1), hyp.sum(1)
    if hyp.shape[1] == 0:
        correct = 0
    else:
        overlap = ref.T.astype(np.int64) @ hyp.astype(np.int64)
        r, h = linear_sum_assignment(-overlap)
        correct = int(np.minimum(ref[:, r], hyp[:, h]).sum()) if len(r) else 0
    total = nref.sum()
    miss = np.maximum(nref - nhyp, 0).sum()
    fa = np.maximum(nhyp - nref, 0).sum()
    conf = np.minimum(nref, nhyp).sum() - correct
    return {"der": (miss + fa + conf) / total, "miss": miss / total, "fa": fa / total, "conf": conf / total}


def run_one(name: str, threads: int) -> dict:
    OUT.mkdir(exist_ok=True)
    audio = BENCH / f"{name}.flac"
    out = OUT / f"{name}-t{threads}.json"
    cmd = ["/usr/bin/time", "-l" if sys.platform == "darwin" else "-v", str(EXE),
           "--input", str(audio), "--output", str(out), "--threads", str(threads)]
    env = dict(os.environ)
    env.setdefault("QUILL_DIARIZER_MODEL", str(BENCH / "model"))
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    wall = time.monotonic() - t0
    if proc.returncode != 0:
        raise SystemExit(f"{name} t{threads} failed:\n{proc.stderr[-3000:]}")
    if sys.platform == "darwin":
        rss = int(re.search(r"(\d+)\s+maximum resident set size", proc.stderr).group(1))
    else:
        rss = int(re.search(r"Maximum resident set size \(kbytes\): (\d+)", proc.stderr).group(1)) * 1024
    doc = json.loads(out.read_text())
    dur = doc["duration"]
    row = {
        "input": name, "threads": threads, "duration_s": dur, "wall_s": round(wall, 1),
        "inference_s": doc["stats"]["inference_s"], "rtf": round(wall / dur, 4),
        "wall_per_hour_min": round(wall / dur * 60, 2), "peak_rss_mib": round(rss / 2**20),
        "speakers": len(doc["speakers"]), "turns": len(doc["turns"]),
        "overlap_turns": sum(t["overlap"] for t in doc["turns"]), "warnings": doc["warnings"],
        "host": f"{platform.machine()} {platform.processor()} {os.cpu_count()} cpus",
    }
    rttm = BENCH / f"{name}.rttm"
    if rttm.exists():
        ref = load_rttm(rttm)
        hyp = [(t["start"], t["end"], t["speaker"]) for t in doc["turns"]]
        row["ref_speakers"] = len({r[2] for r in ref})
        row["der_collar0"] = {k: round(float(v), 4) for k, v in der(ref, hyp, dur, 0.0).items()}
        row["der_collar025"] = {k: round(float(v), 4) for k, v in der(ref, hyp, dur, 0.25).items()}
    with open(OUT / "results.jsonl", "a") as fh:
        fh.write(json.dumps(row) + "\n")
    print(json.dumps(row), flush=True)
    return row


def main(argv: list[str]) -> None:
    for spec in argv:
        name, threads = spec.split(":")
        for t in threads.split(","):
            run_one(name, int(t))


if __name__ == "__main__":
    main(sys.argv[1:])
