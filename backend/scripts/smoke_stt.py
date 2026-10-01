#!/usr/bin/env python3
"""Manual end-to-end smoke test of the live aer-stt-v1 jobs API (NOT a unit test).

    cd backend && .venv/bin/python scripts/smoke_stt.py [--gateway http://localhost:4000/v1]
        [--language en] [--text "..."] [--keep]

Synthesises <= 20 s of speech with macOS ``say`` (or uses --audio FILE), converts
it to 16 kHz mono Opus exactly like the extract stage, checks the catalog status,
submits one job with a Quill-style Idempotency-Key, polls to completion, fetches
the verbose_json result, verifies an idempotent re-submit returns the same job,
prints the response shapes, then DELETEs the job (unless --keep).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quill.pipeline import catalog  # noqa: E402
from quill.pipeline.stt import SttClient, SttError  # noqa: E402

DEFAULT_TEXT = (
    "Good morning everyone. This is the Quill smoke test. "
    "We agreed to ship the transcription pipeline on Friday, and Alice will send the notes."
)


@dataclass(frozen=True)
class Settings:
    gateway_url: str
    stt_model: str


def shape(obj, depth=0):
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://localhost:4000/v1")
    ap.add_argument("--model", default="aer-stt-v1")
    ap.add_argument("--language", default="en")
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--audio", help="use this file instead of `say`")
    ap.add_argument("--keep", action="store_true", help="do not DELETE the job afterwards")
    args = ap.parse_args()
    settings = Settings(args.gateway.rstrip("/"), args.model)

    st = catalog.model_status(settings, args.model, force=True)
    print(f"catalog {catalog.catalog_url(settings)}: {args.model} = {st}")
    if not catalog.is_usable(st):
        print("model not usable; aborting")
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if args.audio:
            src = Path(args.audio)
        else:
            if not shutil.which("say"):
                print("`say` not available; pass --audio")
                return 2
            src = tmp / "speech.aiff"
            subprocess.run(["say", "-o", str(src), args.text], check=True)
        clip = tmp / "clip.opus"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-t", "20", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "32k",
             "-application", "voip", str(clip)],
            check=True,
        )  # fmt: skip
        sha = hashlib.sha256(clip.read_bytes()).hexdigest()
        key = f"quill:smoke-{int(time.time())}:0:{sha}"
        with SttClient(settings, timeout=60) as client:
            t0 = time.monotonic()
            view, fresh = client.submit(clip, model=args.model, language=args.language, key=key)
            print(f"submit -> fresh={fresh} shape={json.dumps(shape(view))}")
            print(f"  job={view['id']} status={view['status']} engine={view.get('engine_model')}")
            jid = view["id"]
            try:
                again, fresh2 = client.submit(clip, model=args.model, language=args.language, key=key)
                print(f"idempotent re-submit -> fresh={fresh2} same_job={again['id'] == jid}")
                delay = 1.0
                while True:
                    view = client.job(jid)
                    print(
                        f"  poll t={time.monotonic() - t0:5.1f}s status={view['status']} "
                        f"progress={view.get('progress')} chunks={view.get('chunks_completed')}/{view.get('chunks_total')}"
                    )
                    if view["status"] in ("completed", "failed", "cancelled"):
                        break
                    if time.monotonic() - t0 > 600:
                        print("timed out waiting")
                        break
                    time.sleep(delay)
                    delay = min(10.0, delay * 1.5)
                if view["status"] == "completed":
                    result = client.result(jid)
                    print(f"result shape={json.dumps(shape(result))}")
                    print(f"result segments={json.dumps(result.get('segments'))[:600]}")
                    print(f"text: {result.get('text')}")
                else:
                    print(f"job ended {view['status']}: {view.get('error')}")
            finally:
                if not args.keep:
                    try:
                        gone = client.delete(jid)
                        print(f"delete -> {gone and gone.get('status')}")
                    except SttError as exc:
                        print(f"delete failed: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
