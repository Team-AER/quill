# Phase 0: Nemotron-3-Diarization on CPU

**Verdict: GO.** The gate is **≤ 10 min of wall time per 1 h of audio on 8 cores**.

Measured on Apple Silicon:

| Recording | Wall time | Share of the gate | Peak RSS |
|---|---|---|---|
| 1 h at 6 threads | **0.59 min** | about 17× under | 0.9 GiB |
| 8.3 h at 6 threads | **4.3 min** | — | **1.1 GiB** |

Speaker identities stay stable across the whole 8 h run. Diarization error on
real far-field meetings is about 17 % DER (collar 0.25 s) and **speaker
confusion is ≤ 0.8 %**. Neither the ONNX nor the NeMo-Speech.cpp fallback is
needed.

- Date: 2026-09-30
- Runtime: PyTorch 2.14.0 CPU, Transformers `5.18.0.dev0` at git `f339035b`, fp32, Python 3.12.14
- Script: `bench/run_bench.py` runs the real CLI (`quill-diarize`) under `/usr/bin/time`, so
  wall time includes process start, imports (about 1.4 s) and model load (about 1.9 s)
- Inference mode: chunked with the checkpoint's offline configuration (30.4 s windows,
  speaker cache 264, FIFO 40, update period 300), with audio streamed from disk

## Host

The results come from an Apple **M2 Max** (8 performance cores and 4 efficiency
cores, 64 GiB RAM, macOS, arm64). Production is **x86-64** (a Proxmox LXC with 6
cores and 12 GiB RAM), so treat these numbers as **indicative only**; see "x86
caveat" below.

## Test audio (real meetings with a reference)

The recordings come from the [AMI Meeting Corpus](https://groups.inf.ed.ac.uk/ami/corpus/)
(CC BY 4.0), SDM (single distant microphone), test split. They were taken from the HF
repackaging [`diarizers-community/ami`](https://huggingface.co/datasets/diarizers-community/ami),
which carries pyannote's word-level reference segmentation. A single far-field
microphone in a meeting room is close to a laptop recording of a meeting, and
harder than close-talk audio. `bench/prepare_ami.py` builds these inputs:

| Input | Content | Duration | Reference speakers |
|---|---|---|---|
| `ami10` | ES2004b, first 600 s | 10.0 min | 4 |
| `ami60` | TS3003a + TS3003b back to back (same 4 people, two sessions) | 61.9 min | 4 |
| `ami480` | `ami60` tiled 8× | 8.26 h | 4 (for throughput, memory and identity stability) |

## Speed and memory

Wall time is for the whole CLI process. RTF is wall time divided by audio duration.

| Input | Threads | Wall | RTF | Min per 1 h audio | Peak RSS |
|---|---|---|---|---|---|
| ami10 (10 min) | 1 | 14.5 s | 0.024 | 1.45 | 892 MiB |
| ami10 | 2 | 10.4 s | 0.017 | 1.04 | 892 MiB |
| ami10 | 4 | 9.3 s | 0.016 | 0.93 | 885 MiB |
| ami10 | 6 | 8.9 s | 0.015 | 0.89 | 890 MiB |
| ami10 | 8 | 10.7 s | 0.018 | 1.07 | 888 MiB |
| **ami60 (62 min)** | 4 | 38.1 s | 0.0102 | **0.61** | 896 MiB |
| **ami60** | **6** | 36.5 s | 0.0098 | **0.59** | 902 MiB |
| **ami60** | 8 | 35.8 s | 0.0096 | **0.58** | 897 MiB |
| **ami480 (8.26 h)** | 6 | 259.6 s (4.3 min) | 0.0087 | 0.52 | **1136 MiB** |

How to read the table:

- **Time.** Each 30.4 s window costs the same, so time grows linearly with
  duration. The 10 min runs are dominated by about 3 s of fixed startup.
- **Threads.** Scaling above 2 threads is flat on this machine. Each forward is a
  short sequence (at most about 684 encoder frames × 512 hidden), so per-op
  overhead dominates, not GEMM throughput. Even **1 thread** does 1 h in
  about 1.3 min.
- **Memory** is essentially constant. About 0.85 GiB is the torch runtime plus
  the model. The rest is the `[T, 8]` float32 probability matrix (92 MB for 8 h)
  and the post-processing buffers. The full recording is never held in memory.
  The worker unit's `MemoryMax=9G` leaves **about 8× headroom** at 8 h.

### x86 caveat

x86-64 performance was **not measured natively**. Apple M2 cores are fast, and a
2016–2020-era x86 server core can be 2–4× slower per core with this workload.
Even a pessimistic **10× slowdown** would give about 6 min per 1 h audio (about
45 min for 8 h), which is still inside the gate.

**Install check under emulation.** The exact install recipe from `README.md`
(`pip install --require-hashes -r requirements-lock-linux-x86_64-cpu.txt`) was run
in an `ubuntu:24.04` **linux/amd64** container. The container ran under OrbStack
with Rosetta emulation, where torch reports CPU capability `DEFAULT`, meaning no
AVX2 or AVX-512. That makes it a worst-case x86 floor:

| Check | Result |
|---|---|
| Install | Clean; the venv is **1.6 GB** |
| Tests | All 30 pass, including the real-model streaming-vs-offline check |
| Fake mode | Works |
| 2 min AMI clip, 4 threads | 11.7 s wall, 1.0 GiB peak RSS |
| 2 min AMI clip, 6 threads | 13.6 s wall |
| Throughput | RTF about 0.1, so about **6 min per 1 h**, still inside the gate while emulated without SIMD |
| Output | Same 4 speakers and 23 turns as the native arm64 run on the same clip |

A native AVX2 or AVX-512 x86 core should be several times faster than this.

The first step on the real LXC should be:

```bash
QUILL_DIARIZER_MODEL=/opt/quill/models/nemotron-3-diarization \
  /usr/bin/time -v /opt/quill/diarizer-venv/bin/quill-diarize --input ami60.flac --output /tmp/t.json --threads 4
```

## Quality

Quality is measured as frame-level DER at 10 ms, with the optimal one-to-one
speaker mapping (Hungarian) and overlap scored. The script is in
`bench/run_bench.py`.

| Input | DER (collar 0) | Miss | False alarm | Confusion | DER (collar 0.25 s) | Speakers found |
|---|---|---|---|---|---|---|
| ami10 | 19.3 % | 14.4 % | 4.1 % | 0.7 % | 17.0 % | 4 of 4 |
| ami60 | 20.8 % | 19.7 % | 0.9 % | 0.2 % | 17.2 % | 4 of 4 |

**Identity across two sessions.** `ami60` joins two separate recordings of the
same four people. The model kept the same four labels across the join: there
were no new slots, and confusion was 0.2 %.

**Identity across 8 h.** Scoring each 1 h copy inside `ami480` against the
reference gives these results:

| Copy | DER | Confusion |
|---|---|---|
| 1 | 20.8 % | 0.22 % |
| 4 | 21.2 % | 0.14 % |
| 8 | 21.0 % | 0.20 % |

Per-speaker talk time per copy was S1 about 1260 s, S2 about 273 s, S3 about 612 s
and S4 about 160 s, the same within ±1 %. The speaker cache holds identities
for the full 8 h.

**Nearly all of the error is missed speech.** The reference marks every word,
including backchannels from quiet far-field speakers. Speaker attribution
itself is nearly perfect.

### Post-processing ablation (same model output)

| Setting | ami10 DER | ami60 DER | Turns (ami60) |
|---|---|---|---|
| Raw threshold 0.5 only | 28.5 % | 30.3 % | — |
| **Default** (0.5, median 11, drop < 0.3 s, merge < 0.8 s) | **19.3 %** | **20.8 %** | 792 |
| Default without the gap merge | 28.6 % | 31.4 % | 1464 |
| Threshold 0.4 | 19.0 % | 19.9 % | 813 |
| Threshold 0.3 | 19.2 % | 18.9 % | 834 |

Notes on the settings:

- **The plan's post-processing** cuts DER by about 10 points. The 0.8 s gap merge
  does most of the work, and it also halves the number of turns, which means
  fewer STT calls downstream.
- **Lower thresholds** trade a little false alarm for less miss, for a gain of
  about 1 point. The default stays at 0.5 as planned, and `--threshold` is
  exposed so it can be tuned on real Team-AER recordings.

## Correctness of the streaming implementation

The CLI feeds 30.4 s windows with the speaker cache carried between them.
Compared with the library's whole-file offline forward on 3 min of AMI audio:

- **Up to the final chunk:** the probabilities are identical (max |Δ| < 1e-3).
- **Final chunk:** it differs slightly, because of spectrogram edge padding at the
  end of the file (max |Δ| 0.12 on a few frames).
- **Overall:** 0.0014 % of binarized frames disagree.

This is covered by `tests/test_engine.py`, which runs when `QUILL_DIARIZER_MODEL`
is set.

## Alternatives considered

- **NeMo (`nemo_toolkit[asr]` 3.0.0):** not needed. It produces the same model
  output with a much larger dependency tree (Lightning, Hydra and more).
  Transformers' native support gives the same checkpoint in fewer packages. The
  catch is that it is currently on git main only.
- **ONNX + onnxruntime:** not needed at this speed. It would also require
  exporting the speaker-cache update (`topk` and gather logic) or keeping it in
  Python.
- **NeMo-Speech.cpp** with the published `q8_0` GGUF: a possible future option if
  the x86 LXC turns out much slower than expected. It is untested here.

## Recommendation for the LXC (6 cores / 12 GiB)

- **Threads:** run with `--threads 4`. That is effectively as fast as 6–8, and it
  leaves cores for the API, ffmpeg and frame work.
- **Memory limit:** a `MemoryMax=3G` on a dedicated diarizer scope is ample. At
  the shared worker's `MemoryMax=9G`, the diarizer uses about 1.1 GiB at 8 h.
- **Disk:** the venv takes about 1.6 GB and the model about 0.4 GB. For
  non-16 kHz input, the temporary transcode needs about 0.5 GB per 8 h, but the
  pipeline's `diar.flac` needs none.
