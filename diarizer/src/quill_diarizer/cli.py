"""quill-diarize: Nemotron-3-Diarization on CPU -> turns.json (docs/CONTRACT.md "Diarizer CLI").

Exit codes: 0 ok, 2 usage, 3 input missing/undecodable, 4 model/runtime load
failure, 5 inference failure, 6 output not writable, 130 interrupted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .postprocess import MODEL_SLOTS, PostParams, postprocess

EXIT_OK, EXIT_USAGE, EXIT_INPUT, EXIT_MODEL, EXIT_INFER, EXIT_OUTPUT = 0, 2, 3, 4, 5, 6


class Progress:
    """Writes `PROGRESS <fraction>` lines to stderr (or --progress-fd), at most every 0.5 s."""

    def __init__(self, fd: int | None):
        self.stream = os.fdopen(fd, "w", buffering=1, closefd=False) if fd is not None else sys.stderr
        self.last_t = 0.0
        self.last_f = -1.0

    def __call__(self, fraction: float, force: bool = False) -> None:
        fraction = min(max(fraction, 0.0), 1.0)
        now = time.monotonic()
        if not force and (now - self.last_t < 0.5 or fraction - self.last_f < 0.001):
            return
        self.last_t, self.last_f = now, fraction
        self.stream.write(f"PROGRESS {fraction:.4f}\n")
        self.stream.flush()


def err(msg: str) -> None:
    sys.stderr.write(f"quill-diarize: {msg}\n")
    sys.stderr.flush()


def default_threads() -> int:
    configured = os.environ.get("QUILL_DIARIZER_THREADS", "")
    if configured.isdigit() and int(configured) > 0:
        return int(configured)
    try:
        return max(1, len(os.sched_getaffinity(0)))  # respects cgroup/cpuset on Linux
    except AttributeError:
        return max(1, os.cpu_count() or 1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quill-diarize", description=__doc__.splitlines()[0])
    p.add_argument("--input", required=True, type=Path, help="audio file, ideally 16 kHz mono FLAC/WAV")
    p.add_argument("--output", required=True, type=Path, help="turns JSON to write (atomically)")
    p.add_argument("--max-speakers", type=int, default=None,
                   help=f"cap on speakers (1-{MODEL_SLOTS}); extra model slots are merged into the nearest kept one")
    p.add_argument("--threads", type=int, default=None, help="torch intra-op threads (default: available CPUs)")
    p.add_argument("--progress-fd", type=int, default=None, help="write PROGRESS lines to this fd instead of stderr")
    p.add_argument("--model", default=None,
                   help="HF model id or local dir (default: $QUILL_DIARIZER_MODEL or nvidia/Nemotron-3-Diarization)")
    p.add_argument("--threshold", type=float, default=PostParams.threshold)
    p.add_argument("--save-probs", type=Path, default=None, help="also save raw [T,8] probabilities (.npy)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_speakers is not None and not 1 <= args.max_speakers <= 64:
        err("--max-speakers must be >= 1")
        return EXIT_USAGE
    threads = args.threads or default_threads()
    fake = os.environ.get("QUILL_DIARIZER_FAKE", "") not in ("", "0", "false", "no")
    progress = Progress(args.progress_fd)
    warnings: list[str] = []
    if args.max_speakers and args.max_speakers > MODEL_SLOTS:
        warnings.append(f"max_speakers={args.max_speakers} exceeds the model's {MODEL_SLOTS} slots")

    if not args.input.is_file():
        err(f"input not found: {args.input}")
        return EXIT_INPUT

    t0 = time.monotonic()
    progress(0.0, force=True)
    try:
        if fake:
            import numpy as np  # noqa: F401  (numpy only; no torch in fake mode)

            from .fake import FAKE_MODEL, FAKE_SPEAKERS, fake_probs, probe_duration

            duration = probe_duration(args.input)
            probs = fake_probs(duration, min(args.max_speakers or FAKE_SPEAKERS, MODEL_SLOTS))
            model_name = FAKE_MODEL
        else:
            from . import engine

            try:
                res = engine.run(args.input, threads, args.model, progress=lambda f: progress(f * 0.98))
            except engine.InputError as exc:
                err(str(exc))
                return EXIT_INPUT
            except engine.ModelError as exc:
                err(str(exc))
                return EXIT_MODEL
            probs, duration, model_name = res.probs, res.duration_s, res.model
    except KeyboardInterrupt:
        err("interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001
        err(f"inference failed: {type(exc).__name__}: {exc}")
        return EXIT_INFER
    infer_s = time.monotonic() - t0

    if args.save_probs:
        import numpy as np

        np.save(args.save_probs, probs.astype("float16"))

    result = postprocess(probs, PostParams(threshold=args.threshold), args.max_speakers)
    warnings += result.warnings
    for w in warnings:
        err(f"warning: {w}")

    doc = {
        "model": model_name,
        "speakers": result.speakers,
        "turns": [t.as_dict() for t in result.turns],
        "overlaps": result.overlaps,
        "warnings": warnings,
        "duration": round(float(duration), 3),
        "stats": {
            "slots_used": result.slots_used,
            "threads": threads,
            "inference_s": round(infer_s, 2),
            "rtf": round(infer_s / duration, 4) if duration > 0 else None,
            "fake": fake,
        },
    }
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_name(args.output.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        os.replace(tmp, args.output)
    except OSError as exc:
        err(f"cannot write output: {exc}")
        return EXIT_OUTPUT
    progress(1.0, force=True)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
