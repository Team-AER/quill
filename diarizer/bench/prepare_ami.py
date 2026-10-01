"""Build benchmark inputs from the AMI corpus (CC BY 4.0) SDM test split.

Source: https://huggingface.co/datasets/diarizers-community/ami (sdm/test-0000{0,1}-of-00003.parquet),
a repackaging of the AMI Meeting Corpus single-distant-microphone recordings with
pyannote "only_words" reference segmentation.

Writes into $QUILL_BENCH_DIR (default ~/.cache/quill-diarizer-bench):
  ami10.flac / ami10.rttm   first 600 s of ES2004b (4 speakers)
  ami60.flac / ami60.rttm   TS3003a + TS3003b back to back (same 4 speakers, ~60 min)
  ami480.flac               ami60 tiled 8x (~8 h) for memory/throughput only (no reference)
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf

BENCH = Path(os.environ.get("QUILL_BENCH_DIR", Path.home() / ".cache" / "quill-diarizer-bench"))
SR = 16000


def load_meetings() -> dict[str, dict]:
    out = {}
    for f in sorted(BENCH.glob("sdm-test-*.parquet")):
        for row in pq.read_table(f).to_pylist():
            name = row["audio"]["path"].split(".")[0]
            audio, sr = sf.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(axis=1)
            assert sr == SR, (name, sr)
            segs = list(zip(row["timestamps_start"], row["timestamps_end"], row["speakers"]))
            out[name] = {"audio": audio, "segs": segs}
    return out


def write_rttm(path: Path, uri: str, segs) -> None:
    with open(path, "w") as fh:
        for s, e, spk in sorted(segs):
            if e > s:
                fh.write(f"SPEAKER {uri} 1 {s:.3f} {e - s:.3f} <NA> <NA> {spk} <NA> <NA>\n")


def main() -> None:
    m = load_meetings()
    print("meetings:", {k: round(len(v["audio"]) / SR / 60, 1) for k, v in m.items()})

    # 10 min
    a = m["ES2004b"]["audio"][: 600 * SR]
    segs = [(s, min(e, 600.0), spk) for s, e, spk in m["ES2004b"]["segs"] if s < 600.0]
    sf.write(BENCH / "ami10.flac", a, SR)
    write_rttm(BENCH / "ami10.rttm", "ami10", segs)

    # 60 min: two sessions of the same group
    parts, segs, off = [], [], 0.0
    for name in ("TS3003a", "TS3003b"):
        x = m[name]
        parts.append(x["audio"])
        segs += [(s + off, e + off, spk) for s, e, spk in x["segs"]]
        off += len(x["audio"]) / SR
    a60 = np.concatenate(parts)
    sf.write(BENCH / "ami60.flac", a60, SR)
    write_rttm(BENCH / "ami60.rttm", "ami60", segs)

    # 8 h: tile, streamed to disk so this script stays small
    with sf.SoundFile(BENCH / "ami480.flac", "w", SR, 1) as fh:
        for _ in range(8):
            fh.write(a60)
    for f in ("ami10.flac", "ami60.flac", "ami480.flac"):
        print(f, round(sf.info(BENCH / f).duration / 60, 1), "min")


if __name__ == "__main__":
    main()
