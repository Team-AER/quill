#!/usr/bin/env python3
"""Live smoke check of Quill's LLM request shapes against llm-proxy.

Makes three small real calls through QUILL_GATEWAY_URL (default
http://localhost:4000/v1): a JSON-schema text call (key moments), a JSON-schema
map call (synthesis) and one vision call on a PIL-drawn "slide". Read-only for
the gateway (GET catalog + POST chat completions). Not part of the unit tests.

    backend/.venv/bin/python backend/scripts/smoke_llm.py [--model MODEL]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quill.config import Settings  # noqa: E402
from quill.pipeline import catalog, llm, moments, synthesis, vision  # noqa: E402

TRANSCRIPT = [
    {"speaker": "S1", "start": 5.0, "end": 12.0, "text": "Hi all, I'm Priya from finance, thanks for joining."},
    {"speaker": "S2", "start": 14.0, "end": 25.0, "text": "Thanks Priya. Let me share my screen, this is the Q3 revenue slide."},
    {"speaker": "S2", "start": 26.0, "end": 40.0, "text": "As you can see revenue grew twelve percent quarter on quarter, mostly from EMEA."},
    {"speaker": "S1", "start": 41.0, "end": 55.0, "text": "Great. So we agree to keep the EMEA pricing as is. Tom, can you send the updated forecast by Friday?"},
    {"speaker": "S3", "start": 56.0, "end": 62.0, "text": "Yes, I'll send the forecast by Friday."},
    {"speaker": "S1", "start": 63.0, "end": 75.0, "text": "One open question is whether APAC needs a separate price list. Let's revisit next week."},
]


def make_slide() -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1280, 720), "white")
    d = ImageDraw.Draw(img)
    try:
        big = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 56)
        small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 34)
    except OSError:
        big = small = ImageFont.load_default()
    d.rectangle([0, 0, 1280, 110], fill=(20, 60, 140))
    d.text((40, 25), "Q3 Revenue Review", fill="white", font=big)
    for i, line in enumerate(["Revenue: $4.2M (+12% QoQ)", "EMEA: $1.9M (+21%)", "APAC: $0.8M (-3%)",
                              "Next step: updated forecast by Friday"]):
        d.text((60, 160 + i * 70), "• " + line, fill="black", font=small)
    bars = [(700, 1.0), (800, 1.4), (900, 1.9)]
    for x, h in bars:
        d.rectangle([x + 150, 620 - int(h * 120), x + 220, 620], fill=(40, 120, 200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="google/gemma-4-12B-it-qat-w4a16-ct")
    args = ap.parse_args()
    s = Settings.from_env()
    print(f"gateway: {s.gateway_url}  model: {args.model}")
    print(f"catalog status: {catalog.model_status(s, args.model)}")
    c = llm.LLMClient(args.model, s.gateway_url, timeout=120, status_lookup=llm.default_status_lookup(s))
    ok = True

    t0 = time.monotonic()
    found = moments.text_candidates(c, TRANSCRIPT, concurrency=1)
    print(f"\n[text/json_schema moments] {time.monotonic() - t0:.2f}s schema_mode={c.schema_mode}")
    print(json.dumps(found, indent=1))
    ok &= bool(found)

    t0 = time.monotonic()
    notes = synthesis.synthesize(c, TRANSCRIPT, [], [{"label": "S1", "display_name": None}], 80)
    print(f"\n[text/json_schema map+reduce] {time.monotonic() - t0:.2f}s")
    print(json.dumps(notes, indent=1))
    ok &= bool(notes["chapters"])

    slide = Path(__file__).resolve().parent / ".smoke_slide.jpg"
    slide.write_bytes(make_slide())
    try:
        lat = []
        for i in range(2):
            t0 = time.monotonic()
            res = vision.analyse_frame(c, slide, 30.0, "Q3 revenue slide", "screen share of revenue",
                                       vision.context_lines(TRANSCRIPT, 30.0))
            lat.append(time.monotonic() - t0)
        print(f"\n[vision/json_schema] latencies {', '.join(f'{x:.2f}s' for x in lat)}")
        print(json.dumps(res, indent=1))
        ok &= res["kind"] in vision.KINDS and "12" in (res["visible_text"] + " ".join(res["key_facts"]))
    finally:
        slide.unlink(missing_ok=True)
    print("\nSMOKE", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
