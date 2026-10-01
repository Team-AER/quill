# Quick-Quotes Quill — implementation plan

> Team AER meeting intelligence. Drop a meeting recording (video up to 10 GB, or
> audio), get a speaker-attributed transcript, visual key moments, a summary and
> structured notes. Unlike Rita Skeeter's quill, this one is supposed to quote
> people accurately.

- Repo: [`Team-AER/quill`](https://github.com/Team-AER/quill)
- Date: 2026-09-30 · Status: the original plan, written before the build and kept
  as design rationale. The shipped app differs in places; the README and
  docs/ACCOUNTS.md describe what exists.

---

## 1. What exists today (verified 2026-09-30)

| Dependency | State | Consequence for Quill |
|---|---|---|
| `aer-stt-v1` via the llm-proxy gateway (`/v1`) | Live. Omnilingual 3B, with English hints routed to Qwen3-ASR 1.7B. Durable jobs API `/v1/audio/transcriptions/jobs` with `Idempotency-Key`, progress, cancel and retry. | Use the jobs API, always through the gateway, never the inference host directly. |
| STT limits | 2 GiB upload, 8 h duration, 4 jobs per user, **chunk-level timestamps only**, **no diarization**, no word alignment. nginx on llm-proxy allows a 2050 MB body for this route. | Send extracted audio (Opus), never the video. Speaker attribution has to come from our own diarization (§4.3). |
| Model enable/disable | Avifors polls the proxy catalog every 5 s. A disabled or quarantined model rejects new uploads and pauses running jobs at a chunk boundary, then resumes when re-enabled. | Quill reads the same catalog status, shows "STT paused by admin", holds its own queue and never counts a pause as a failure (§4.4). |
| `google/gemma-4-12B-it-qat-w4a16-ct` | Live, 131k context. **Vision works** since a gateway config fix on 2026-09-24. The catalog `capabilities` has no `vision` entry. | Use it for frame analysis and summaries. Add `"vision"` to its catalog capabilities in the gateway. |
| `nvidia/Nemotron-3-Diarization` | 100M-parameter Sortformer (31 layers, RoPE, AOSC speaker cache). 16 kHz mono input, **≤ 8 speakers**, chunked or streaming with no length limit, per-frame speaker probabilities at 10 ms. Runs in NeMo, Transformers or NeMo-Speech.cpp. OpenMDW-1.1 licence. The card only benchmarks NVIDIA GPUs. | CPU speed is the main unknown, so Phase 0 benchmarks it before anything else is built. |
| Public ingress | The public hostname is DNS-only (no CDN request-body cap) and lands on a reverse proxy (NPMplus). | A 10 GB upload is possible if we send it in resumable chunks (§4.1). |

## 2. Architecture

```
Browser ──tus chunks──► reverse proxy (TLS) ─────────► quill LXC (CPU only)
                                                     ├─ api      FastAPI: auth, tus upload, REST, SSE progress
                                                     ├─ worker   job runner (SQLite queue, resumable stages)
                                                     │    ├─ ffmpeg / ffprobe
                                                     │    ├─ diarizer subprocess (separate venv: torch-cpu + NeMo)
                                                     │    └─ HTTP → llm-proxy gateway
                                                     │          ├─ aer-stt-v1   (jobs API)
                                                     │          └─ gemma-4-12B  (vision + text)
                                                     └─ web      React + Vite static build, served by api
Storage: /var/lib/quill/{uploads,media,frames,db}
```

The stack follows **Erised** (FastAPI + SQLite + React/Vite, a systemd unit, and
`deploy/provision-lxc.sh` / `deploy-lxc.sh` / `install-release.sh`), because
Python is the natural home for NeMo and ffmpeg. The diarizer runs in its **own
venv as a subprocess**, so the torch/NeMo dependency tree never touches the API.
It also gets its own `MemoryMax=` and CPU quota.

## 3. Job model

A meeting has one pipeline. Each stage is idempotent and checkpointed in
SQLite, so a crash or redeploy resumes from the last finished stage:

```
uploaded → probed → audio_extracted → diarized → transcribed
        → [video only] key_moments → frames_analyzed
        → synthesized → done
```

- The **mode** is `video` or `audio`. It is auto-detected by ffprobe and can be
  overridden at upload ("treat as audio only"). Audio mode skips both video stages.
- Stage status is one of `pending | running | paused(reason) | failed(error) | done`,
  with a progress fraction and an ETA. Progress is pushed to the UI over SSE.
- One worker process runs one meeting at a time by default. The Nemotron CPU
  step is the bottleneck, and STT calls run concurrently inside a meeting
  (§4.4). Every stage can be re-run from the UI ("re-diarize with 3 speakers",
  "re-summarize").

## 4. Pipeline stages

### 4.1 Upload (10 GB)
- Uses the **tus resumable upload protocol**: `tus-js-client` in the browser and
  a small tus endpoint in FastAPI (or `tusd` behind it), with 64 MB chunks. That
  way no single request is large, NPMplus needs only a modest
  `client_max_body_size`, and a dropped Wi-Fi connection resumes instead of
  restarting.
- Enforced server-side: a 10 GiB hard cap (configurable), a free-disk
  reservation before accepting, a checksum at the end, and a container sniff
  with ffprobe instead of trusting the extension.
- NPMplus proxy host: `proxy_request_buffering off`, `client_max_body_size 128m`,
  long read timeouts, websockets on (for the SSE fallback).

### 4.2 Probe + audio extraction (ffmpeg, CPU)
- `ffprobe` reads duration, streams and codecs, and picks the mode.
- One decode pass produces two outputs:
  - `diar.flac`: 16 kHz mono PCM/FLAC for Nemotron.
  - `stt.opus`: 16 kHz mono Opus at 32 kbps for STT, about 115 MB for 8 h and
    well under the 2 GiB limit.
- A loudness-normalized (`loudnorm`) variant is optional. Test whether it helps
  STT on quiet rooms before making it the default.
- The source video is kept only as long as the frames stage needs it, then
  under a retention setting (§7).

### 4.3 Diarization (Nemotron-3-Diarization on CPU)
- Chunked offline inference with the model's speaker cache, so identities stay
  consistent across hours. The chunk and latency settings come from the Phase 0
  benchmark. Offline mode (30 s chunks) should be far cheaper than streaming.
- Post-processing:
  1. Threshold the per-speaker probabilities (default 0.5), then apply a median
     filter.
  2. Drop segments under 0.3 s.
  3. Merge same-speaker gaps under 0.8 s.
  4. Mark overlap regions, where two speakers are active at once.
- Output is **turns**: `{speaker, start, end, overlap:bool}`, stored in the DB
  and exported as RTTM.
- **More than 8 speakers**: the model has 8 slots. Quill warns when all 8 are in
  use and accepts that very large meetings will merge similar voices. Speaker
  embedding clustering could be a later option.
- **CPU risk**: the model card lists no CPU support. Fallbacks, in order:
  1. torch-cpu + NeMo with `torch.set_num_threads(cores)`.
  2. An ONNX export of the Sortformer encoder run in onnxruntime-CPU (the same
     approach Erised used for Real-ESRGAN).
  3. NeMo-Speech.cpp.
  Phase 0 decides which one ships.

### 4.4 Transcription (`aer-stt-v1` through llm-proxy)
STT gives only chunk timestamps, so the two outputs can't be matched after the
fact. We **transcribe per speaker turn instead**:

1. Build **transcription segments** from turns:
   - Merge consecutive same-speaker turns.
   - Pad each segment by ±0.25 s.
   - Absorb micro-turns under 1.2 s ("yeah", "mm-hm") into the surrounding
     segment as an interjection marker, so they don't become their own STT calls.
   - Split segments longer than 10 min at the quietest point, so one retry
     never re-runs a huge span.
2. Cut each segment from `stt.opus` with `ffmpeg -ss/-to -c copy`, which is
   cheap and needs no re-encode.
3. Submit each segment to `POST /v1/audio/transcriptions/jobs` with:
   - `model=aer-stt-v1`
   - `language` hint from the upload form (`auto` omits it, `en` routes to Qwen3-ASR)
   - `response_format=verbose_json`
   - `Idempotency-Key: quill:{meeting}:{segment}:{sha256(clip)}`
   Up to **3 in flight**, one under Avifors' per-user cap of 4 so an admin or
   CLI user can still get through. Poll with backoff, then fetch the result.
4. **Model controls**:
   - Before submitting, and every 30 s during the stage, read
     `GET <gateway>/v1/models` plus the catalog status that the
     proxy tools container exposes.
   - When `aer-stt-v1` is disabled or quarantined, the stage goes to
     `paused("STT model disabled in llm-proxy")`. Quill submits nothing new, and
     jobs Avifors already accepted keep their checkpoints and resume
     automatically.
   - Rejected-while-disabled responses (409/503) count as a pause, not a failure.
   - A settings toggle lets the admin choose `aer-stt-v1` or `aer-stt-qwen3`.
     Only models whose catalog status is `ready` or `degraded` can be selected.
5. **Alternative strategy (config flag, benchmarked in Phase 0)**: one
   whole-file job, with speakers assigned by the overlap between STT chunks and
   diarization turns. This uses fewer GPU jobs but gets speaker changes inside a
   20–30 s chunk wrong. Per-turn is the default unless the benchmark shows it's
   too slow on real meetings.
6. The result is transcript lines `{speaker, start, end, text, overlap, interjections[]}`.

### 4.5 Key moments (video mode only)
1. **Candidate moments from text**: Gemma reads the transcript in windows of
   about 15 min and returns JSON (guided by a schema in `response_format`). Each
   item is `{t, why, look_for}`: moments where something is *shown*, such as
   "as you can see on this slide", screen shares, demos, whiteboards, numbers or
   charts, and decisions.
2. **Candidate moments from pixels**: an ffmpeg scene-change pass
   (`select='gt(scene,0.3)'` on a 2 fps, 480p proxy stream) finds slide flips
   and screen-share changes cheaply on CPU.
3. **Fuse**:
   - Snap each text moment to the nearest scene change within ±20 s.
   - Keep scene-change clusters that last over 30 s even when no text points at
     them (long-lived slides).
   - Dedupe by perceptual hash (pHash) so the same slide isn't analyzed twice.
   - **Budget**: about 40 frames per hour, capped at 200 per meeting, both
     configurable.
4. If there's **no transcript** (STT failed or was skipped), fall back to pure
   scene change plus a fixed 1-per-3-min sampling. This is the "if provided"
   case.

### 4.6 Frame analysis (Gemma 4 vision)
- For each moment:
  - Extract the frame (JPEG, long edge 1280, plus a thumbnail).
  - Send it to `google/gemma-4-12B-it-qat-w4a16-ct` as `image_url` (base64),
    together with the ±60 s of transcript around it and the `look_for` hint.
- Structured output: `{kind: slide|screen|whiteboard|people|demo|other,
  title, visible_text, key_facts[], relevance: 0-3, caption}`.
- Frames with relevance 0 are dropped from the notes but kept in the gallery.
  `visible_text` becomes searchable.
- Concurrency 2, `enable_thinking` off (via `chat_template_kwargs`), 60 s timeout, 2 retries.

### 4.7 Synthesis (summary, notes)
A meeting of up to 8 h (roughly 130k tokens) doesn't fit in one prompt with
headroom, so this is a **map-reduce**:
- **Map**: for each chapter-sized window (about 20 min, split at speaker-turn
  boundaries), Gemma writes a section summary, decisions, action items, open
  questions, and a chapter title with a timestamp. It receives the frame
  analyses that fall inside the window.
- **Reduce**: one call merges the sections into the final notes:
  1. TL;DR (3–5 bullets)
  2. Executive summary
  3. Chapters, with timestamps linking into the player
  4. Decisions
  5. **Action items** `{owner, task, due?, t}`
  6. Open questions / risks
  7. Key visuals (frame thumbnails and captions)
  8. Suggested speaker names
- **Speaker names**: the LLM proposes a name for "Speaker 2" from self-introductions and
  forms of address, with the evidence timestamp. The user confirms or renames in
  the UI, and a rename propagates everywhere without re-running the LLM.
- Every claim carries a **source timestamp** so each note links back to the
  moment it came from. Anything the LLM says that doesn't resolve to a real
  transcript span is flagged.
- The text model is configurable. The default is Gemma 4 (the same model is
  already warm for vision). Qwen3.8-Flash-Next is an option, with the Hedwig
  primary→Gemma fallback pattern.

## 5. Web app

**Library**: meeting cards (title, date, duration, mode, speakers, status pill)
and a drag-and-drop zone with per-file resumable progress. Upload options: title,
language hint, expected speaker count (optional), and audio-only.

**Meeting view**:
- **Player**: video (HLS isn't needed, the original is served with HTTP range
  requests) or a waveform in audio mode.
- **Transcript** (right column): speaker-colored and synced. Clicking a line
  seeks the player, the current line auto-scrolls, and search covers transcript
  text and on-screen text from frames.
- **Tabs**: Summary · Notes · Action items · Key moments (frame gallery) · Speakers (rename, merge two speakers).
- **Pipeline strip**: stage status, ETA, and paused reasons, e.g. the "STT disabled in llm-proxy" banner.

**Exports**: Markdown notes, transcript as TXT/SRT/VTT (speaker-labelled) and
JSON, and RTTM for diarization.

**Design**: follow the Hedwig/Erised visual language (glass UI, not
serif-editorial). Do a design pass through a Design artifact before the UI build.

## 6. Data model (SQLite, WAL)
`users`, `meetings`, `stages`, `media_assets`, `speakers(id, meeting, label,
display_name)`, `turns`, `segments(stt_job_id, idempotency_key, status)`,
`transcript_lines`, `moments`, `frames`, `notes(json, version)`, `events` (audit
and progress). FTS5 index over transcript lines and frame text.

## 7. Security, privacy, retention
- Meetings are sensitive, so login is required (same auth pattern as Erised) and
  there are no public share links in v1.
- Transcripts and frames are never logged. Only IDs and timings go into traces,
  matching the Avifors policy.
- **Retention** is set per instance:
  - Source video: deleted after N days (default 14), or keep forever.
  - Audio, transcript, frames and notes: kept until the user deletes them.
  - Avifors drops its own copy 48 h after a job ends.
- Deleting a meeting also cancels in-flight Avifors jobs (`DELETE /jobs/{id}`)
  and removes its files.

## 8. Infrastructure
| Item | Proposal |
|---|---|
| Container | A dedicated, unprivileged Proxmox LXC, Ubuntu 24.04, **6 cores / 12 GiB RAM**, 32 GB root + **200 GB** data disk at /var/lib/quill. CPU only. |
| Network | A DHCP reservation (or a static address) so the container has a stable LAN name. |
| Public | A reverse-proxy host (NPMplus) with a Let's Encrypt certificate, forced HTTPS, websockets, and the upload settings from §4.1. |
| llm-proxy | Add `"vision"` to Gemma's catalog `capabilities`, and a `quill` consumer contract. |
| Deploy | `deploy/provision-lxc.sh`, `deploy-lxc.sh` and `install-release.sh`, cloned from Erised. Everything is installed inside the LXC and nothing on the Proxmox host (files are streamed through `pct exec`). |

## 9. Phased delivery

**Phase 0: spikes (go/no-go, about 1 day)**
1. Nemotron on CPU: measure real-time factor on 8 cores for a 1 h and a 4 h
   meeting, for NeMo torch-cpu versus an ONNX export. **Gate**: diarization of
   1 h in ≤ 10 min on 8 cores (better than 6×).
2. STT strategy: per-turn versus whole-file with overlap assignment on 2 real
   meetings. Compare wall-clock time, GPU jobs, and speaker-attribution errors
   checked by hand.
3. Gemma vision: 20 real meeting frames (slides, screen share, faces) to check
   caption and OCR quality.
4. Upload: 10 GB through the reverse proxy with tus, including a forced disconnect and
   resume.

**Phase 1: audio MVP**: upload → extract → diarize → transcribe → transcript
viewer with speaker rename, plus local dev loop and tests (fake STT/LLM servers,
as with Erised's fake engine).

**Phase 2: notes**: map-reduce synthesis, action items, chapters, exports, and search.

**Phase 3: video**: key-moment fusion, frame analysis, gallery, notes enriched with visuals.

**Phase 4: ship**: LXC provisioning, DNS and the reverse proxy, the llm-proxy catalog
change, backups (SQLite backup API plus a systemd timer like Erised's), and a
design polish pass.

**Acceptance for v1**
- A 2 h, 10 GB-class screen-share recording completes end to end with no
  manual steps.
- Disabling `aer-stt-v1` in llm-proxy mid-run pauses Quill with a visible
  reason. Re-enabling it resumes with no duplicated transcript lines.
- A worker restart mid-diarization or mid-transcription resumes from the last
  checkpoint.
- Every action item and decision links to a real timestamp.

## 10. Risks
| Risk | Mitigation |
|---|---|
| Nemotron too slow on CPU | ONNX/C++ runtime; more cores; offline 30 s chunks; or a GPU host later |
| More than 8 speakers | Warn the user; clustering fallback later |
| Per-turn STT loses cross-sentence context and pays scheduler overhead | Micro-turn absorption, segments ≥ 1.2 s, the whole-file alternative behind a flag |
| Shared GPU contention (Gemma serves Hedwig and Erised too) | Low concurrency (2–3), Avifors latency-mode yielding, runs are background jobs |
| Hallucinated notes | Timestamp grounding and a flag on anything that doesn't resolve |
| Disk exhaustion | Reserve before accepting an upload, then retention sweeps |

## 11. Open decisions (defaults in bold)
1. Auth: **single admin plus invited users (Erised pattern)**, or SSO.
2. Source-video retention: **14 days**, forever, or delete right after processing.
3. Summary model: **Gemma 4**, or Qwen primary with Gemma fallback.
4. LXC size: decided, 6 cores / 12 GiB / 200 GB.
