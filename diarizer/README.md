# quill-diarizer

Speaker diarization for Quill (area B, see `../docs/PLAN.md` §4.3 and the
"Diarizer CLI" section of `../docs/CONTRACT.md`). It runs
[`nvidia/Nemotron-3-Diarization`](https://huggingface.co/nvidia/Nemotron-3-Diarization)
(100M-parameter Streaming Sortformer, up to 8 speakers, OpenMDW-1.1) on **CPU only**
and writes speaker turns as JSON.

The package lives in its own venv, so torch never enters the API/worker environment.
The worker calls it as a subprocess.

## Runtime path

The runtime is **PyTorch CPU plus Hugging Face Transformers**
(`AutoModelForAudioFrameClassification`). NeMo is not needed, and neither is
ONNX: the Phase 0 gate passes by a wide margin (see `BENCHMARK.md`).

- The model runs chunk by chunk with the checkpoint's **offline** settings: 30.4 s
  windows (340 + 40 encoder frames), a FIFO of 40, and a speaker-cache update
  period of 300. The Arrival-Order Speaker Cache carries speaker identities
  across chunks, so they stay stable over 8 h.
- Audio is read from disk one window at a time with a `soundfile` seek, and
  never loaded whole. Memory is about 0.9 GiB for 10 min and about 1.1 GiB for 8 h.
- The output is identical to the library's whole-file offline forward, except in
  the last chunk. There, spectrogram edge padding causes a difference in about
  0.001 % of binarized frames (`tests/test_engine.py`).
- If the input isn't 16 kHz mono (for example an opus or mp4 file), it is
  transcoded once with `ffmpeg` into a temporary FLAC. The pipeline's `diar.flac`
  is already 16 kHz mono, so it needs no conversion.

## CLI

```
quill-diarize --input diar.flac --output turns.json [--max-speakers N] [--threads N]
              [--progress-fd FD] [--model ID_OR_DIR] [--threshold 0.5] [--save-probs probs.npy]
```

**Output.** The output file is written atomically (`.tmp` then rename):

```json
{"model": "/opt/quill/models/nemotron-3-diarization",
 "speakers": ["S1", "S2", "S3"],
 "turns": [{"speaker": "S1", "start": 0.51, "end": 12.62, "overlap": false}],
 "overlaps": [{"start": 12.1, "end": 12.62, "speakers": ["S1", "S2"]}],
 "warnings": [],
 "duration": 3715.947,
 "stats": {"slots_used": 3, "threads": 6, "inference_s": 36.2, "rtf": 0.0097, "fake": false}}
```

The contract requires `model`, `speakers` and `turns`. The other fields are
extras: `overlaps`, `warnings`, `duration` and `stats`.

**Progress.** `PROGRESS <fraction>` lines go to stderr, at most every 0.5 s. They
start at `PROGRESS 0.0000` and end at `PROGRESS 1.0000`. With `--progress-fd N`
they go to that file descriptor instead.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Usage error |
| 3 | Input missing or undecodable |
| 4 | Model or runtime could not be loaded |
| 5 | Inference failed |
| 6 | Output not writable |
| 130 | Interrupted |

Every non-zero exit prints `quill-diarize: <message>` on stderr.

**Model location.** The model comes from `--model`, else `$QUILL_DIARIZER_MODEL`,
else `nvidia/Nemotron-3-Diarization` from the Hugging Face cache. In production,
point it at a local directory and set `HF_HUB_OFFLINE=1`.

**Threads.** The default is the number of CPUs the process may use (this
respects the cgroup cpuset).

**Fake mode.** `QUILL_DIARIZER_FAKE=1` gives deterministic fake turns based
only on the input duration. It needs no torch, runs the real post-processing,
and reports the model as `fake/quill-diarizer`. It uses 3 speakers by default,
or `--max-speakers` if given.

### Post-processing

The code is in `src/quill_diarizer/postprocess.py`. It uses numpy only and is
unit-tested. The steps run in this order:

1. **Threshold.** A frame is active when its probability is above 0.5 (`--threshold`).
2. **Median filter.** An 11-frame (110 ms) filter over time, computed as an exact
   majority vote with a cumulative sum.
3. **Drop short segments.** Segments under 0.3 s are removed.
4. **Merge gaps.** Same-speaker gaps under 0.8 s are merged.
5. **`--max-speakers N` (optional).** The N channels with the most speech are kept.
   Each extra channel is folded into the kept channel with the highest mean
   probability over its frames, and a warning is added.
6. **Mark overlaps.** A turn gets `overlap: true` when it shares at least 0.2 s with
   another speaker's turn. The exact regions are also listed in `overlaps`.
7. **Relabel.** Speakers are renamed `S1..Sn` in order of first appearance.
8. **Warn on full slots.** When all 8 model slots are in use, a warning goes to
   stderr and into `warnings`: meetings with more than 8 people get similar voices
   merged.

## Install on production (Ubuntu 24.04 LXC, x86-64, no GPU)

These steps install inside the LXC only. The lock file pins the PyTorch **CPU**
wheel (`torch==2.14.0+cpu` from `download.pytorch.org/whl/cpu`, so no CUDA
libraries are pulled). It also pins Transformers to the commit that was
benchmarked, as a GitHub source tarball, because Nemotron3Diarization is not in a
PyPI release yet (5.18.0.dev0). The tarball needs no git. Every entry, the tarball
included, is sha256-hashed. When 5.18 ships, replace the tarball line with
`transformers==5.18.*`.

```bash
apt-get install -y python3.12-venv libsndfile1 ffmpeg
python3 -m venv /opt/quill/diarizer-venv
/opt/quill/diarizer-venv/bin/pip install --require-hashes -r diarizer/requirements-lock-linux-x86_64-cpu.txt
/opt/quill/diarizer-venv/bin/pip install --no-deps ./diarizer

# The model is about 400 MB. Fetch it once, then run offline.
mkdir -p /opt/quill/models/nemotron-3-diarization && cd $_
for f in config.json processor_config.json model.safetensors; do
  curl -fL -o $f https://huggingface.co/nvidia/Nemotron-3-Diarization/resolve/main/$f
done

# The worker runs it with:
#   QUILL_DIARIZER_MODEL=/opt/quill/models/nemotron-3-diarization HF_HUB_OFFLINE=1
#   /opt/quill/diarizer-venv/bin/quill-diarize --input diar.flac --output turns.json --threads 4
```

**Venv size.** The installed venv is about 1.6 GB (torch-cpu, plus
librosa/numba/scipy for the feature extractor's mel filterbank).

**Regenerating the lock.** Edit `requirements.in`, then run:

```bash
uv pip compile requirements.in --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple \
  --index-strategy unsafe-best-match --emit-index-url --generate-hashes --no-header \
  -o requirements-lock-linux-x86_64-cpu.txt
```

## Development (macOS)

```bash
brew install python@3.12 ffmpeg
/opt/homebrew/bin/python3.12 -m venv diarizer/.venv
diarizer/.venv/bin/pip install -e 'diarizer[model,test]'   # or just [test] for fake mode
diarizer/.venv/bin/python -m pytest diarizer/tests -q
# Real-model equivalence test:
QUILL_DIARIZER_MODEL=~/.cache/quill-diarizer-bench/model diarizer/.venv/bin/python -m pytest diarizer/tests -q
```

On macOS, pip pulls the standard torch wheel. Only Linux needs the CPU index.

## Benchmark

`bench/prepare_ami.py` builds 10 min, 62 min and 8.3 h inputs from the AMI corpus
(CC BY 4.0). `bench/run_bench.py` runs them and writes wall time, peak RSS and DER
to `bench/out/`, which is gitignored. The results are in `BENCHMARK.md`. Audio
and weights live in `~/.cache/quill-diarizer-bench`, never in the repo.
