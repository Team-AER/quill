# Quill internal contract (read before touching code)

Implements docs/PLAN.md. Several people build this in parallel; every module
has one owner. Do not edit files outside your area; if the contract must change,
say so in your final report instead of changing another area.

## Layout and ownership

```
backend/                      Python 3.12, FastAPI, SQLite (stdlib sqlite3), no ORM
  pyproject.toml              [A] deps for api+worker (NOT torch/nemo)
  quill/__init__.py           [A]
  quill/config.py             [A] Settings from env QUILL_* (see below)
  quill/db.py                 [A] schema + connection helpers + migrations
  quill/auth.py               [A] Erised-style: admin bootstrap, invited users, session cookie
  quill/users.py              account, people and invite routes + `quill-users` CLI (docs/ACCOUNTS.md)
  quill/sharing.py            meeting sharing (view/edit, everyone) + directory (docs/ACCOUNTS.md)
  quill/upload.py             [A] tus 1.0.0 server (creation, termination, checksum sha256)
  quill/api.py / app.py       [A] REST + SSE, serves frontend/dist
  quill/worker.py             [A] job runner: runs stages in order, resume, pause handling
  quill/stages.py             [A] StageContext, StagePaused, StageFailed, STAGES registry
  quill/pipeline/media.py     [C1] probe, extract audio, cut clips, frames, scene detection
  quill/pipeline/diarize.py   [C1] runs the diarizer subprocess, stores turns
  quill/pipeline/segments.py  [C1] turns -> transcription segments (pure functions)
  quill/pipeline/catalog.py   [C1] llm-proxy model status (ready/degraded/disabled)
  quill/pipeline/stt.py       [C1] aer-stt-v1 jobs API client + transcribe stage
  quill/pipeline/llm.py       [C2] chat client for llm-proxy (json schema, images, retries)
  quill/pipeline/moments.py   [C2] key-moment detection + fusion stage
  quill/pipeline/vision.py    [C2] frame analysis stage
  quill/pipeline/synthesis.py [C2] map-reduce notes stage
  tests/                      each owner adds test_<module>.py; fakes, no network
diarizer/                     [B] standalone package, own venv (torch-cpu + nemo)
frontend/                     [D] React 19 + Vite + TypeScript
deploy/                       [E] provision/deploy/install scripts, systemd units
```

## Config (env, all prefixed QUILL_)
`DATA_DIR` (/var/lib/quill), `GATEWAY_URL` (http://localhost:4000/v1),
`STT_MODEL` (aer-stt-v1), `VISION_MODEL` and `TEXT_MODEL`
(google/gemma-4-12B-it-qat-w4a16-ct), `DIARIZER_CMD`
(/opt/quill/diarizer-venv/bin/quill-diarize), `MAX_UPLOAD_BYTES` (10 GiB),
`VIDEO_RETENTION_DAYS` (14), `STT_CONCURRENCY` (3), `VISION_CONCURRENCY` (2),
`FRAMES_PER_HOUR` (40), `MAX_FRAMES` (200), `SECRET_KEY`, `PUBLIC_URL`.
Settings is a frozen dataclass: `from quill.config import Settings; s = Settings.from_env()`.

## Data directory
`{DATA_DIR}/db/quill.sqlite3`, `{DATA_DIR}/uploads/<upload_id>` (tus partials),
`{DATA_DIR}/media/<meeting_id>/{source.<ext>, diar.flac, stt.opus, clips/, frames/, proxy.mp4}`.
`ctx.media_dir` is the per-meeting directory.

## Stage contract (quill/stages.py, owned by A)
```python
class StagePaused(Exception):   # reason: str; worker sets status paused, retries after 30 s
class StageFailed(Exception):   # message: str; worker marks failed, user can retry

@dataclass
class StageContext:
    meeting_id: str
    settings: Settings
    media_dir: Path
    mode: str                        # "video" | "audio"
    db: Callable[[], sqlite3.Connection]   # opens a new connection (row_factory=Row)
    def progress(self, fraction: float, detail: str = "") -> None: ...
    def should_stop(self) -> bool: ...     # user cancelled/deleted
    def log(self, message: str) -> None: ...   # never transcript text

STAGES = ["probe", "extract_audio", "diarize", "transcribe",
          "key_moments", "frames", "synthesize"]   # key_moments/frames skipped when mode == "audio"
# each is `def run(ctx: StageContext) -> None`, idempotent: it must clean/replace its own
# prior output rows before writing, and may reuse finished sub-work (e.g. done STT segments).
```
Stage modules expose `run`: probe/extract_audio -> media.run_probe / media.run_extract,
diarize -> diarize.run, transcribe -> stt.run, key_moments -> moments.run,
frames -> vision.run, synthesize -> synthesis.run.
The worker imports them lazily by that mapping.

## Schema (quill/db.py, owned by A; others read and write these tables only)
```sql
users(id INTEGER PK, email TEXT UNIQUE, password_hash TEXT, is_admin INT, created_at TEXT)
sessions(token TEXT PK, user_id INT, expires_at TEXT)
invites(token TEXT PK, email TEXT, created_by INT, expires_at TEXT, used_at TEXT)
-- migration 2 (docs/ACCOUNTS.md) adds user profile/status, session device info, invite ids/roles/withdrawal, password_resets
meetings(id TEXT PK, owner_id INT, title TEXT, created_at TEXT, mode TEXT, language TEXT,
         expected_speakers INT, duration_s REAL, source_name TEXT, source_bytes INT,
         source_path TEXT, has_video INT, width INT, height INT, status TEXT,
         error TEXT, source_deleted_at TEXT)
stages(meeting_id TEXT, name TEXT, status TEXT, progress REAL, detail TEXT,
       started_at TEXT, finished_at TEXT, error TEXT, PRIMARY KEY(meeting_id,name))
speakers(meeting_id TEXT, label TEXT, display_name TEXT, suggested_name TEXT,
         suggestion_evidence_t REAL, color INT, PRIMARY KEY(meeting_id,label))  -- label "S1".."S8"
turns(id INTEGER PK, meeting_id TEXT, speaker TEXT, start REAL, end REAL, overlap INT)
segments(id INTEGER PK, meeting_id TEXT, idx INT, speaker TEXT, start REAL, end REAL,
         clip_sha256 TEXT, idempotency_key TEXT, stt_job_id TEXT, status TEXT,
         text TEXT, engine_model TEXT, error TEXT, interjections TEXT)  -- interjections JSON [{speaker,start,end}]
transcript_lines(id INTEGER PK, meeting_id TEXT, speaker TEXT, start REAL, end REAL,
         text TEXT, overlap INT, segment_id INT)
moments(id INTEGER PK, meeting_id TEXT, t REAL, source TEXT, why TEXT, look_for TEXT, phash TEXT)
frames(id INTEGER PK, meeting_id TEXT, moment_id INT, t REAL, path TEXT, thumb_path TEXT,
       kind TEXT, title TEXT, visible_text TEXT, key_facts TEXT, relevance INT, caption TEXT)
notes(meeting_id TEXT PK, version INT, json TEXT, model TEXT, created_at TEXT)
events(id INTEGER PK, meeting_id TEXT, at TEXT, kind TEXT, message TEXT)
-- FTS5: transcript_fts(text, meeting_id UNINDEXED, line_id UNINDEXED), frame_fts(visible_text, caption, ...)
```
Times are seconds from the start of the recording (float). Timestamps in ISO-8601 UTC.
The DB helpers `quill.db.connect(path)` and `quill.db.init(path)` exist; tests use a
tmp path and `init`.

## Notes JSON (written by synthesis, rendered by frontend)
```json
{"tldr": ["..."], "summary": "markdown",
 "chapters": [{"title": "", "start": 0.0, "end": 0.0, "summary": ""}],
 "decisions": [{"text": "", "t": 0.0, "grounded": true}],
 "action_items": [{"owner": "S2|name|null", "task": "", "due": null, "t": 0.0, "grounded": true}],
 "open_questions": [{"text": "", "t": 0.0, "grounded": true}],
 "key_visuals": [{"frame_id": 1, "t": 0.0, "caption": ""}],
 "speaker_suggestions": [{"label": "S2", "name": "", "evidence_t": 0.0}]}
```

## Diarizer CLI (owned by B)
`quill-diarize --input diar.flac --output turns.json [--max-speakers N] [--threads N] [--progress-fd 3]`
writes `{"model": "...", "speakers": ["S1",...], "turns": [{"speaker": "S1", "start": 0.0, "end": 1.2, "overlap": false}]}`
and prints progress lines `PROGRESS <fraction>` to stderr. Exit 0 on success, non-zero with a
message on stderr otherwise. `QUILL_DIARIZER_FAKE=1` env produces deterministic fake turns
(for dev and tests without torch).

## HTTP API (owned by A; frontend D consumes)
All under `/api`, JSON, cookie session `quill_session`. Errors `{"error": "..."}`.
- `POST /api/auth/login {email,password}`, `POST /api/auth/logout`, `GET /api/auth/me`
- `POST /api/auth/setup {email,password}` only when no users exist; `GET /api/auth/state -> {needs_setup}`
- Invites, password resets, your account and the admin People routes: see docs/ACCOUNTS.md (supersedes the original `POST /api/invites {email}` shape)
- tus: `OPTIONS|POST /api/uploads`, `HEAD|PATCH|DELETE /api/uploads/{id}`. Upload-Metadata keys:
  `filename, title, language, expected_speakers, audio_only`. On completion the server creates the
  meeting and returns header `Quill-Meeting-Id`.
- `GET /api/meetings`, `GET /api/meetings/{id}` (meeting + stages + speakers),
  `PATCH /api/meetings/{id} {title}`, `DELETE /api/meetings/{id}`
- `GET /api/meetings/{id}/transcript` -> lines[], `GET /api/meetings/{id}/notes`,
  `GET /api/meetings/{id}/frames`, `GET /api/meetings/{id}/moments`
- `PATCH /api/meetings/{id}/speakers/{label} {display_name}`; `POST /api/meetings/{id}/speakers/merge {from,into}`
- `POST /api/meetings/{id}/rerun {stage, options}` (reruns stage and all later stages)
- `GET /api/meetings/{id}/media` (range requests, source or stt.opus if source deleted),
  `GET /api/frames/{frame_id}/image?thumb=1`
- `GET /api/meetings/{id}/export?format=md|txt|srt|vtt|json|rttm`
- `GET /api/search?q=` -> transcript + frame hits
- `GET /api/events` SSE: `event: meeting` data `{id,status,stages:[...]}`
- `GET /api/system` -> model status from catalog ({stt:{model,status}, vision:..., text:...}), disk free
- `GET /api/settings`, `PATCH /api/settings` (admin; stt_model choice restricted to ready/degraded)
