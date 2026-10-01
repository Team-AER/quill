"""REST + SSE API (everything under /api except auth and tus, which live in
auth.py and upload.py). Meetings are private to their owner."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from . import db
from .auth import current_user, get_db, require_admin, user_from_request
from .config import Settings
from .stages import STAGES, VIDEO_ONLY_STAGES, now_iso
from .sharing import RANK, VISIBLE, VISIBLE_M, access_of, meeting_access, my_shares, people_by_id, sharing_fields
from .worker import purge_meeting

router = APIRouter(prefix="/api")

STT_MODEL_CHOICES = ("aer-stt-v1", "aer-stt-qwen3")
SELECTABLE_STATUSES = ("ready", "degraded")
MEDIA_TYPES = {".opus": "audio/ogg", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".mkv": "video/x-matroska",
               ".webm": "video/webm", ".mp4": "video/mp4", ".mov": "video/quicktime", ".mp3": "audio/mpeg",
               ".wav": "audio/wav", ".flac": "audio/flac", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".png": "image/png", ".webp": "image/webp"}


# ------------------------------------------------------------ helpers

def _settings(request: Request) -> Settings:
    return request.app.state.settings


NEEDS = {"edit": "You can view this meeting but not change it. Ask its owner for edit access.",
         "owner": "Only the person who uploaded this meeting can do that."}


def get_meeting(conn: sqlite3.Connection, meeting_id: str, user: dict, need: str = "view") -> sqlite3.Row:
    """The meeting if `user` may open it (own, shared with them, or shared with everyone) with at
    least `need` access ('view' < 'edit' < 'owner'). 404 when they can't see it at all."""
    row = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if row is None or row["status"] in ("deleting", "cancelled"):
        raise HTTPException(404, "Meeting not found.")
    access = meeting_access(conn, row, user["id"])
    if access is None:
        raise HTTPException(404, "Meeting not found.")
    if RANK[access] < RANK[need]:
        raise HTTPException(403, NEEDS[need])
    return row


def stages_json(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, Any]]:
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM stages WHERE meeting_id=?", (meeting_id,))}
    out = []
    for name in STAGES:
        r = rows.get(name)
        out.append({
            "name": name,
            "status": r["status"] if r else "pending",
            "progress": float(r["progress"] or 0) if r else 0.0,
            "detail": r["detail"] if r else None,
            "started_at": r["started_at"] if r else None,
            "finished_at": r["finished_at"] if r else None,
            "error": r["error"] if r else None,
        })
    return out


def overall_progress(stages: list[dict]) -> tuple[str | None, float]:
    relevant = [s for s in stages if s["status"] != "skipped"]
    if not relevant:
        return None, 1.0
    current = next((s["name"] for s in relevant if s["status"] != "done"), None)
    total = sum(1.0 if s["status"] == "done" else s["progress"] for s in relevant)
    return current, round(total / len(relevant), 4)


def meeting_json(row: sqlite3.Row, stages: list[dict] | None = None) -> dict[str, Any]:
    out = {k: row[k] for k in ("id", "title", "created_at", "mode", "language", "expected_speakers",
                               "duration_s", "source_name", "source_bytes", "width", "height", "status",
                               "error", "source_deleted_at")}
    out["has_video"] = None if row["has_video"] is None else bool(row["has_video"])
    if stages is not None:
        out["current_stage"], out["progress"] = overall_progress(stages)
    return out


def speaker_name(label: str, display: str | None) -> str:
    if display:
        return display
    m = re.fullmatch(r"S(\d+)", label or "")
    return f"Speaker {m.group(1)}" if m else (label or "Unknown")


def speakers_json(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, Any]]:
    rows = {r["label"]: r for r in conn.execute("SELECT * FROM speakers WHERE meeting_id=?", (meeting_id,))}
    labels = set(rows)
    labels |= {r[0] for r in conn.execute("SELECT DISTINCT speaker FROM turns WHERE meeting_id=? AND speaker IS NOT NULL",
                                          (meeting_id,))}
    labels |= {r[0] for r in conn.execute(
        "SELECT DISTINCT speaker FROM transcript_lines WHERE meeting_id=? AND speaker IS NOT NULL", (meeting_id,))}
    talk = {r[0]: r[1] for r in conn.execute(
        "SELECT speaker, sum(end-start) FROM turns WHERE meeting_id=? GROUP BY speaker", (meeting_id,))}

    def order(label: str):
        m = re.fullmatch(r"S(\d+)", label)
        return (0, int(m.group(1)), label) if m else (1, 0, label)

    out = []
    for i, label in enumerate(sorted(labels, key=order)):
        r = rows.get(label)
        display = r["display_name"] if r else None
        out.append({
            "label": label,
            "display_name": display,
            "name": speaker_name(label, display),
            "suggested_name": r["suggested_name"] if r else None,
            "suggestion_evidence_t": r["suggestion_evidence_t"] if r else None,
            "color": r["color"] if r and r["color"] is not None else i,
            "talk_time_s": round(float(talk.get(label) or 0.0), 2),
        })
    return out


def _json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def transcript_json(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT l.*, s.interjections AS seg_interjections FROM transcript_lines l "
        "LEFT JOIN segments s ON s.id=l.segment_id WHERE l.meeting_id=? ORDER BY l.start, l.id",
        (meeting_id,)).fetchall()
    return [{"id": r["id"], "speaker": r["speaker"], "start": r["start"], "end": r["end"], "text": r["text"] or "",
             "overlap": bool(r["overlap"]), "segment_id": r["segment_id"],
             "interjections": _json_list(r["seg_interjections"])} for r in rows]


def frames_json(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM frames WHERE meeting_id=? ORDER BY t, id", (meeting_id,)).fetchall()
    return [{"id": r["id"], "moment_id": r["moment_id"], "t": r["t"], "kind": r["kind"], "title": r["title"],
             "visible_text": r["visible_text"], "key_facts": _json_list(r["key_facts"]),
             "relevance": r["relevance"], "caption": r["caption"],
             "image_url": f"/api/frames/{r['id']}/image",
             "thumb_url": f"/api/frames/{r['id']}/image?thumb=1"} for r in rows]


def moments_json(conn: sqlite3.Connection, meeting_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM moments WHERE meeting_id=? ORDER BY t, id", (meeting_id,)).fetchall()
    return [{"id": r["id"], "t": r["t"], "source": r["source"], "why": r["why"], "look_for": r["look_for"]}
            for r in rows]


def notes_row(conn: sqlite3.Connection, meeting_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM notes WHERE meeting_id=?", (meeting_id,)).fetchone()
    if r is None:
        return None
    try:
        notes = json.loads(r["json"]) if r["json"] else None
    except ValueError:
        notes = None
    return {"version": r["version"], "model": r["model"], "created_at": r["created_at"], "notes": notes}


# ------------------------------------------------------------ meetings

@router.get("/meetings")
def list_meetings(user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(f"SELECT * FROM meetings WHERE {VISIBLE} AND status NOT IN ('deleting','cancelled')"
                        " ORDER BY created_at DESC, id", (user["id"], user["id"])).fetchall()
    shares = my_shares(conn, user["id"])
    people = people_by_id(conn)
    out = []
    for row in rows:
        item = meeting_json(row, stages_json(conn, row["id"]))
        item.update(sharing_fields(conn, row, access_of(row, user["id"], shares.get(row["id"])) or "view", people))
        item["speaker_count"] = conn.execute(
            "SELECT count(DISTINCT speaker) FROM turns WHERE meeting_id=?", (row["id"],)).fetchone()[0]
        item.update(card_extras(conn, row["id"]))
        out.append(item)
    return out


def card_extras(conn: sqlite3.Connection, meeting_id: str) -> dict[str, Any]:
    """What a library card shows beyond the row: a cover frame, who spoke, and a one-line gist."""
    cover = conn.execute(
        "SELECT id FROM frames WHERE meeting_id=? ORDER BY CASE WHEN coalesce(relevance, 1) > 0 THEN 0 ELSE 1 END,"
        " t, id LIMIT 1", (meeting_id,)).fetchone()
    speakers = [{k: s[k] for k in ("label", "display_name", "name", "color", "talk_time_s")}
                for s in speakers_json(conn, meeting_id)]
    gist, actions = None, None
    notes = (notes_row(conn, meeting_id) or {}).get("notes")
    if isinstance(notes, dict):
        tldr = [t for t in notes.get("tldr") or [] if isinstance(t, str) and t.strip()]
        summary = notes.get("summary")
        summary = summary.strip() if isinstance(summary, str) else ""
        gist = tldr[0].strip() if tldr else (re.split(r"(?<=[.!?])\s", summary, maxsplit=1)[0] or None)
        if gist:
            gist = re.sub(r"[*_`#]+", "", gist)[:280]
        actions = len(notes.get("action_items") or [])
    return {"cover_url": f"/api/frames/{cover['id']}/image?thumb=1" if cover else None,
            "speakers": speakers, "gist": gist, "action_count": actions}


def meeting_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    stages = stages_json(conn, row["id"])
    out = meeting_json(row, stages)
    out["stages"] = stages
    out["speakers"] = speakers_json(conn, row["id"])
    out["has_notes"] = conn.execute("SELECT 1 FROM notes WHERE meeting_id=?", (row["id"],)).fetchone() is not None
    return out


@router.get("/meetings/{meeting_id}")
def get_meeting_detail(meeting_id: str, user: dict = Depends(current_user),
                       conn: sqlite3.Connection = Depends(get_db)):
    return detail_for(conn, get_meeting(conn, meeting_id, user), user)


def detail_for(conn: sqlite3.Connection, row: sqlite3.Row, user: dict) -> dict[str, Any]:
    out = meeting_detail(conn, row)
    out.update(sharing_fields(conn, row, meeting_access(conn, row, user["id"]) or "view"))
    return out


@router.patch("/meetings/{meeting_id}")
def patch_meeting(meeting_id: str, body: dict = Body(...), user: dict = Depends(current_user),
                  conn: sqlite3.Connection = Depends(get_db)):
    get_meeting(conn, meeting_id, user, "edit")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200:
        raise HTTPException(400, "Title must be 1-200 characters.")
    with conn:
        conn.execute("UPDATE meetings SET title=? WHERE id=?", (title.strip(), meeting_id))
    return detail_for(conn, get_meeting(conn, meeting_id, user), user)


@router.delete("/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, request: Request, user: dict = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_db)):
    get_meeting(conn, meeting_id, user, "owner")
    with conn:
        conn.execute("UPDATE meetings SET status='deleting' WHERE id=?", (meeting_id,))
        running = conn.execute("SELECT 1 FROM stages WHERE meeting_id=? AND status='running'",
                               (meeting_id,)).fetchone()
    if not running:
        purge_meeting(_settings(request), meeting_id)
    # else: the worker notices via ctx.should_stop(), stops the stage and purges.
    return {"ok": True}


@router.get("/meetings/{meeting_id}/transcript")
def get_transcript(meeting_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    get_meeting(conn, meeting_id, user)
    return transcript_json(conn, meeting_id)


@router.get("/meetings/{meeting_id}/notes")
def get_notes(meeting_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    get_meeting(conn, meeting_id, user)
    return notes_row(conn, meeting_id) or {"version": None, "model": None, "created_at": None, "notes": None}


@router.get("/meetings/{meeting_id}/frames")
def get_frames(meeting_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    get_meeting(conn, meeting_id, user)
    return frames_json(conn, meeting_id)


@router.get("/meetings/{meeting_id}/moments")
def get_moments(meeting_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    get_meeting(conn, meeting_id, user)
    return moments_json(conn, meeting_id)


@router.get("/meetings/{meeting_id}/events")
def get_events(meeting_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Addition: the meeting's audit/progress log (never contains transcript text)."""
    get_meeting(conn, meeting_id, user)
    rows = conn.execute("SELECT id, at, kind, message FROM events WHERE meeting_id=? ORDER BY id DESC LIMIT 200",
                        (meeting_id,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ speakers

SPEAKER_LABEL = re.compile(r"[A-Za-z0-9_-]{1,16}")


def _speaker_exists(conn: sqlite3.Connection, meeting_id: str, label: str) -> bool:
    return any(s["label"] == label for s in speakers_json(conn, meeting_id))


@router.patch("/meetings/{meeting_id}/speakers/{label}")
def rename_speaker(meeting_id: str, label: str, body: dict = Body(...), user: dict = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_db)):
    get_meeting(conn, meeting_id, user, "edit")
    if not SPEAKER_LABEL.fullmatch(label) or not _speaker_exists(conn, meeting_id, label):
        raise HTTPException(404, "Speaker not found.")
    name = body.get("display_name")
    if name is not None and not isinstance(name, str):
        raise HTTPException(400, "display_name must be a string or null.")
    name = (name or "").strip()[:100] or None
    with conn:
        conn.execute("INSERT INTO speakers(meeting_id, label, display_name) VALUES(?,?,?) "
                     "ON CONFLICT(meeting_id, label) DO UPDATE SET display_name=excluded.display_name",
                     (meeting_id, label, name))
    return speakers_json(conn, meeting_id)


def _rewrite_interjections(value: str | None, src: str, dst: str) -> str | None:
    items = _json_list(value)
    if not items:
        return value
    for item in items:
        if isinstance(item, dict) and item.get("speaker") == src:
            item["speaker"] = dst
    return json.dumps(items)


@router.post("/meetings/{meeting_id}/speakers/merge")
def merge_speakers(meeting_id: str, body: dict = Body(...), user: dict = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_db)):
    get_meeting(conn, meeting_id, user, "edit")
    src, dst = body.get("from"), body.get("into")
    if not isinstance(src, str) or not isinstance(dst, str) or src == dst:
        raise HTTPException(400, "Provide two different speakers: {from, into}.")
    if not _speaker_exists(conn, meeting_id, src) or not _speaker_exists(conn, meeting_id, dst):
        raise HTTPException(404, "Speaker not found.")
    with conn:
        for table in ("turns", "segments", "transcript_lines"):
            conn.execute(f"UPDATE {table} SET speaker=? WHERE meeting_id=? AND speaker=?", (dst, meeting_id, src))
        for seg in conn.execute("SELECT id, interjections FROM segments WHERE meeting_id=? AND interjections IS NOT NULL",
                                (meeting_id,)).fetchall():
            new = _rewrite_interjections(seg["interjections"], src, dst)
            if new != seg["interjections"]:
                conn.execute("UPDATE segments SET interjections=? WHERE id=?", (new, seg["id"]))
        src_row = conn.execute("SELECT * FROM speakers WHERE meeting_id=? AND label=?", (meeting_id, src)).fetchone()
        conn.execute("INSERT OR IGNORE INTO speakers(meeting_id, label) VALUES(?,?)", (meeting_id, dst))
        if src_row is not None:
            # Keep the source's name/suggestion only where the target has none.
            conn.execute("UPDATE speakers SET display_name=coalesce(display_name, ?),"
                         " suggested_name=coalesce(suggested_name, ?),"
                         " suggestion_evidence_t=coalesce(suggestion_evidence_t, ?) WHERE meeting_id=? AND label=?",
                         (src_row["display_name"], src_row["suggested_name"], src_row["suggestion_evidence_t"],
                          meeting_id, dst))
        conn.execute("DELETE FROM speakers WHERE meeting_id=? AND label=?", (meeting_id, src))
        # Notes reference speakers by label (action item owners, suggestions).
        note = conn.execute("SELECT json FROM notes WHERE meeting_id=?", (meeting_id,)).fetchone()
        if note and note["json"]:
            try:
                data = json.loads(note["json"])
                for item in data.get("action_items") or []:
                    if isinstance(item, dict) and item.get("owner") == src:
                        item["owner"] = dst
                data["speaker_suggestions"] = [s for s in data.get("speaker_suggestions") or []
                                               if not (isinstance(s, dict) and s.get("label") == src)]
                conn.execute("UPDATE notes SET json=? WHERE meeting_id=?", (json.dumps(data), meeting_id))
            except (ValueError, AttributeError):
                pass
        db.add_event(conn, meeting_id, "speakers", f"merged {src} into {dst}")
    return speakers_json(conn, meeting_id)


# ------------------------------------------------------------ rerun

@router.post("/meetings/{meeting_id}/rerun")
def rerun(meeting_id: str, body: dict = Body(...), user: dict = Depends(current_user),
          conn: sqlite3.Connection = Depends(get_db)):
    meeting = get_meeting(conn, meeting_id, user, "owner")
    stage = body.get("stage")
    options = body.get("options") or {}
    if stage not in STAGES:
        raise HTTPException(400, f"stage must be one of {', '.join(STAGES)}.")
    if not isinstance(options, dict):
        raise HTTPException(400, "options must be an object.")
    if meeting["mode"] == "audio" and stage in VIDEO_ONLY_STAGES:
        raise HTTPException(400, "That stage does not apply to audio-only meetings.")
    updates: dict[str, Any] = {}
    if "expected_speakers" in options:
        es = options["expected_speakers"]
        if es is not None and (not isinstance(es, int) or not 1 <= es <= 8):
            raise HTTPException(400, "expected_speakers must be 1-8 or null.")
        updates["expected_speakers"] = es
    if "language" in options:
        lang = options["language"]
        if lang is not None and (not isinstance(lang, str) or len(lang) > 16):
            raise HTTPException(400, "language must be a short language code or null.")
        updates["language"] = None if lang in (None, "", "auto") else lang
    later = STAGES[STAGES.index(stage):]
    with conn:
        for name in later:
            # Probe may switch the mode, so only a later rerun keeps video stages skipped.
            status = "skipped" if (meeting["mode"] == "audio" and name in VIDEO_ONLY_STAGES
                                   and stage != "probe") else "pending"
            conn.execute("UPDATE stages SET status=?, progress=0, detail=NULL, error=NULL, started_at=NULL,"
                         " finished_at=NULL WHERE meeting_id=? AND name=?", (status, meeting_id, name))
        conn.execute("UPDATE stages SET options=? WHERE meeting_id=? AND name=?",
                     (json.dumps(options) if options else None, meeting_id, stage))
        for key, value in updates.items():
            conn.execute(f"UPDATE meetings SET {key}=? WHERE id=?", (value, meeting_id))
        conn.execute("UPDATE meetings SET status='queued', error=NULL WHERE id=?", (meeting_id,))
        db.add_event(conn, meeting_id, "rerun", f"rerun from {stage}")
    return detail_for(conn, get_meeting(conn, meeting_id, user), user)


# ------------------------------------------------------------ media

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _media_type(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _iter_file(path: Path, start: int, length: int, chunk: int = 1024 * 1024) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        fh.seek(start)
        remaining = length
        while remaining > 0:
            data = fh.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def ranged_file_response(request: Request, path: Path, media_type: str | None = None) -> Response:
    size = path.stat().st_size
    media_type = media_type or _media_type(path)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    header = request.headers.get("range")
    if not header:
        headers["Content-Length"] = str(size)
        return StreamingResponse(_iter_file(path, 0, size), media_type=media_type, headers=headers)
    m = RANGE_RE.match(header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:  # suffix range: last N bytes
        start = max(0, size - int(m.group(2)))
        end = size - 1
    end = min(end, size - 1)
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    length = end - start + 1
    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(length)})
    return StreamingResponse(_iter_file(path, start, length), status_code=206, media_type=media_type,
                             headers=headers)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


@router.get("/meetings/{meeting_id}/media")
def get_media(meeting_id: str, request: Request, variant: str = Query("auto"),
              user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Source recording (or stt.opus once the source is deleted). variant=source|audio|proxy|auto."""
    meeting = get_meeting(conn, meeting_id, user)
    settings = _settings(request)
    media_dir = settings.media_dir(meeting_id)
    candidates: list[Path] = []
    source = Path(meeting["source_path"]) if meeting["source_path"] and not meeting["source_deleted_at"] else None
    if variant in ("auto", "source") and source is not None:
        candidates.append(source)
    if variant == "proxy":
        candidates.append(media_dir / "proxy.mp4")
    if variant in ("auto", "audio"):
        candidates.append(media_dir / "stt.opus")
    for path in candidates:
        if path.is_file() and _inside(path, settings.data_dir):
            return ranged_file_response(request, path)
    raise HTTPException(404, "Media is not available.")


@router.get("/frames/{frame_id}/image")
def frame_image(frame_id: int, request: Request, thumb: int = Query(0), user: dict = Depends(current_user),
                conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT f.* FROM frames f WHERE f.id=?", (frame_id,)).fetchone()
    meeting = row and conn.execute("SELECT * FROM meetings WHERE id=?", (row["meeting_id"],)).fetchone()
    if (row is None or meeting is None or meeting["status"] in ("deleting", "cancelled")
            or meeting_access(conn, meeting, user["id"]) is None):
        raise HTTPException(404, "Frame not found.")
    settings = _settings(request)
    raw = (row["thumb_path"] if thumb and row["thumb_path"] else None) or row["path"]
    if not raw:
        raise HTTPException(404, "Frame image missing.")
    path = Path(raw)
    if not path.is_absolute():
        path = settings.media_dir(row["meeting_id"]) / path
    if not _inside(path, settings.data_dir) or not path.is_file():
        raise HTTPException(404, "Frame image missing.")
    return FileResponse(path, media_type=_media_type(path), headers={"Cache-Control": "private, max-age=86400"})


# ------------------------------------------------------------ exports

def fmt_clock(t: float | None) -> str:
    t = max(0.0, float(t or 0.0))
    s = int(t)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_ts(t: float | None, sep: str) -> str:
    ms = int(round(max(0.0, float(t or 0.0)) * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def export_txt(lines: list[dict], names: dict[str, str]) -> str:
    return "".join(f"[{fmt_clock(l['start'])}] {names.get(l['speaker'], speaker_name(l['speaker'], None))}: {l['text']}\n"
                   for l in lines)


def export_srt(lines: list[dict], names: dict[str, str]) -> str:
    out = []
    for i, l in enumerate(lines, 1):
        who = names.get(l["speaker"], speaker_name(l["speaker"], None))
        out.append(f"{i}\n{fmt_ts(l['start'], ',')} --> {fmt_ts(l['end'], ',')}\n{who}: {l['text']}\n")
    return "\n".join(out)


def export_vtt(lines: list[dict], names: dict[str, str]) -> str:
    out = ["WEBVTT\n"]
    for l in lines:
        who = names.get(l["speaker"], speaker_name(l["speaker"], None)).replace(">", "")
        text = (l["text"] or "").replace("-->", "->")
        out.append(f"{fmt_ts(l['start'], '.')} --> {fmt_ts(l['end'], '.')}\n<v {who}>{text}\n")
    return "\n".join(out)


def export_rttm(meeting_id: str, turns: list[sqlite3.Row]) -> str:
    return "".join(
        f"SPEAKER {meeting_id} 1 {float(t['start']):.3f} {max(0.0, float(t['end']) - float(t['start'])):.3f}"
        f" <NA> <NA> {t['speaker']} <NA> <NA>\n" for t in turns)


def export_md(meeting: sqlite3.Row, notes: dict | None, lines: list[dict], names: dict[str, str],
              frames: list[dict]) -> str:
    def who(owner: Any) -> str:
        if not owner:
            return "Unassigned"
        return names.get(owner, str(owner))

    def ts(t: Any) -> str:
        return f"[{fmt_clock(t)}]" if isinstance(t, (int, float)) else ""

    def flag(item: dict) -> str:
        return "" if item.get("grounded", True) else " _(unverified)_"

    md = [f"# {meeting['title'] or 'Meeting'}", ""]
    meta = [f"- Date: {(meeting['created_at'] or '')[:10]}"]
    if meeting["duration_s"]:
        meta.append(f"- Duration: {fmt_clock(meeting['duration_s'])}")
    if names:
        meta.append("- Speakers: " + ", ".join(names.values()))
    md += meta + [""]
    if notes:
        if notes.get("tldr"):
            md += ["## TL;DR", ""] + [f"- {b}" for b in notes["tldr"]] + [""]
        if notes.get("summary"):
            md += ["## Summary", "", str(notes["summary"]).strip(), ""]
        if notes.get("chapters"):
            md += ["## Chapters", ""]
            for c in notes["chapters"]:
                md.append(f"- {ts(c.get('start'))} **{c.get('title', '')}**" + (f": {c['summary']}" if c.get("summary") else ""))
            md.append("")
        if notes.get("decisions"):
            md += ["## Decisions", ""] + [f"- {d.get('text', '')} {ts(d.get('t'))}{flag(d)}" for d in notes["decisions"]] + [""]
        if notes.get("action_items"):
            md += ["## Action items", ""]
            for a in notes["action_items"]:
                due = f" (due {a['due']})" if a.get("due") else ""
                md.append(f"- [ ] **{who(a.get('owner'))}**: {a.get('task', '')}{due} {ts(a.get('t'))}{flag(a)}")
            md.append("")
        if notes.get("open_questions"):
            md += ["## Open questions", ""] + [f"- {q.get('text', '')} {ts(q.get('t'))}{flag(q)}"
                                               for q in notes["open_questions"]] + [""]
        if notes.get("key_visuals"):
            md += ["## Key visuals", ""]
            for v in notes["key_visuals"]:
                md.append(f"- {ts(v.get('t'))} {v.get('caption', '')}")
            md.append("")
    if lines:
        md += ["## Transcript", ""]
        for l in lines:
            md.append(f"**{names.get(l['speaker'], speaker_name(l['speaker'], None))}** {ts(l['start'])}: {l['text']}  ")
        md.append("")
    return "\n".join(md)


@router.get("/meetings/{meeting_id}/export")
def export(meeting_id: str, format: str = Query("md"), user: dict = Depends(current_user),
           conn: sqlite3.Connection = Depends(get_db)):
    meeting = get_meeting(conn, meeting_id, user)
    fmt = format.lower()
    if fmt not in ("md", "txt", "srt", "vtt", "json", "rttm"):
        raise HTTPException(400, "format must be one of md, txt, srt, vtt, json, rttm.")
    speakers = speakers_json(conn, meeting_id)
    names = {s["label"]: s["name"] for s in speakers}
    lines = transcript_json(conn, meeting_id)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", meeting["title"] or "meeting").strip("-")[:80] or "meeting"
    media = {"md": "text/markdown", "txt": "text/plain", "srt": "application/x-subrip", "vtt": "text/vtt",
             "json": "application/json", "rttm": "text/plain"}[fmt]
    if fmt == "txt":
        body = export_txt(lines, names)
    elif fmt == "srt":
        body = export_srt(lines, names)
    elif fmt == "vtt":
        body = export_vtt(lines, names)
    elif fmt == "rttm":
        turns = conn.execute("SELECT * FROM turns WHERE meeting_id=? ORDER BY start, id", (meeting_id,)).fetchall()
        body = export_rttm(meeting_id, turns)
    elif fmt == "json":
        nr = notes_row(conn, meeting_id)
        body = json.dumps({"meeting": meeting_json(meeting), "speakers": speakers, "transcript": lines,
                           "notes": nr["notes"] if nr else None, "moments": moments_json(conn, meeting_id),
                           "frames": frames_json(conn, meeting_id)}, indent=2, ensure_ascii=False)
    else:
        nr = notes_row(conn, meeting_id)
        body = export_md(meeting, nr["notes"] if nr else None, lines, names, frames_json(conn, meeting_id))
    return Response(body, media_type=f"{media}; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{slug}.{fmt}"'})


# ------------------------------------------------------------ search

def fts_query(q: str) -> str | None:
    tokens = re.findall(r"\w+", q, flags=re.UNICODE)[:12]
    if not tokens:
        return None
    return " ".join(f'"{t}"*' for t in tokens)


@router.get("/search")
def search(q: str = Query(""), meeting_id: str | None = Query(None), limit: int = Query(50, ge=1, le=200),
           user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Hits: snippet has matches wrapped in [[ ]] (plain text, not HTML)."""
    query = fts_query(q)
    if not query or len(q.strip()) < 2:
        return {"transcript": [], "frames": []}
    scope = f"{VISIBLE_M} AND m.status NOT IN ('deleting','cancelled')"
    params: list[Any] = [user["id"], user["id"]]
    if meeting_id:
        scope += " AND m.id=?"
        params.append(meeting_id)
    t_rows = conn.execute(
        "SELECT l.id, l.meeting_id, l.speaker, l.start, l.end, l.text, m.title,"
        " snippet(transcript_fts, 0, '[[', ']]', '…', 16) AS snip"
        " FROM transcript_fts JOIN transcript_lines l ON l.id=transcript_fts.rowid"
        " JOIN meetings m ON m.id=l.meeting_id"
        f" WHERE transcript_fts MATCH ? AND {scope} ORDER BY rank LIMIT ?",
        [query, *params, limit]).fetchall()
    f_rows = conn.execute(
        "SELECT f.id, f.meeting_id, f.t, f.caption, f.title AS ftitle, m.title,"
        " snippet(frame_fts, -1, '[[', ']]', '…', 16) AS snip"
        " FROM frame_fts JOIN frames f ON f.id=frame_fts.rowid JOIN meetings m ON m.id=f.meeting_id"
        f" WHERE frame_fts MATCH ? AND {scope} ORDER BY rank LIMIT ?",
        [query, *params, limit]).fetchall()
    display = {}
    for r in conn.execute("SELECT meeting_id, label, display_name FROM speakers WHERE meeting_id IN "
                          f"({','.join('?' * len({r['meeting_id'] for r in t_rows})) or 'NULL'})",
                          list({r["meeting_id"] for r in t_rows})):
        display[(r["meeting_id"], r["label"])] = r["display_name"]
    return {
        "transcript": [{"meeting_id": r["meeting_id"], "meeting_title": r["title"], "line_id": r["id"],
                        "speaker": r["speaker"],
                        "speaker_name": speaker_name(r["speaker"], display.get((r["meeting_id"], r["speaker"]))),
                        "start": r["start"], "end": r["end"], "text": r["text"], "snippet": r["snip"]}
                       for r in t_rows],
        "frames": [{"meeting_id": r["meeting_id"], "meeting_title": r["title"], "frame_id": r["id"], "t": r["t"],
                    "title": r["ftitle"], "caption": r["caption"], "snippet": r["snip"],
                    "thumb_url": f"/api/frames/{r['id']}/image?thumb=1"} for r in f_rows],
    }


# ------------------------------------------------------------ SSE

def meeting_events_snapshot(settings: Settings, user_id: int, meeting_id: str | None = None) -> dict[str, dict]:
    with db.opened(settings.db_path) as conn:
        sql = f"SELECT * FROM meetings WHERE {VISIBLE} AND status NOT IN ('deleting','cancelled')"
        params: list[Any] = [user_id, user_id]
        if meeting_id:
            sql += " AND id=?"
            params.append(meeting_id)
        out = {}
        for row in conn.execute(sql, params).fetchall():
            stages = stages_json(conn, row["id"])
            current, progress = overall_progress(stages)
            out[row["id"]] = {"id": row["id"], "status": row["status"], "error": row["error"], "title": row["title"],
                              "mode": row["mode"], "duration_s": row["duration_s"], "current_stage": current,
                              "progress": progress, "stages": stages}
        return out


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.get("/events")
async def events(request: Request, meeting_id: str | None = Query(None), once: int = Query(0),
                 user: dict = Depends(current_user)):
    """SSE: `event: meeting` with {id,status,stages,...} whenever it changes (polled ~1 s).
    A deleted meeting is sent once as {id, status: "deleted"}. `once=1` sends the snapshot and closes."""
    settings = _settings(request)
    interval = getattr(request.app.state, "sse_interval", 1.0)

    async def stream():
        last: dict[str, str] = {}
        idle = 0.0
        yield "retry: 3000\n\n"
        while True:
            snap = await asyncio.to_thread(meeting_events_snapshot, settings, user["id"], meeting_id)
            for mid, payload in snap.items():
                key = json.dumps(payload, sort_keys=True)
                if last.get(mid) != key:
                    last[mid] = key
                    yield _sse("meeting", payload)
                    idle = 0.0
            for mid in [m for m in last if m not in snap]:
                del last[mid]
                yield _sse("meeting", {"id": mid, "status": "deleted", "stages": []})
            if once:
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(interval)
            idle += interval
            if idle >= 15:
                idle = 0.0
                yield ": keepalive\n\n"
                if user_from_request(request) is None:  # session revoked
                    return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------ system + settings

def catalog_statuses(settings: Settings) -> Any:
    """Call quill.pipeline.catalog.model_statuses lazily; None if unavailable."""
    try:
        from .pipeline import catalog  # type: ignore[attr-defined]
    except Exception:
        return None
    fn = getattr(catalog, "model_statuses", None)
    if fn is None:
        return None
    try:
        return fn(settings)
    except Exception:
        return None


def status_of(statuses: Any, model: str, role: str | None = None) -> str:
    """Accepts {model_id: "ready"}, {model_id: {"status": ...}} or {role: {"model", "status"}}."""
    if not isinstance(statuses, dict):
        return "unknown"
    for key in ((role,) if role else ()) + (model,):
        value = statuses.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("status"), str):
            if key == role and value.get("model") not in (None, model):
                continue
            return value["status"]
    # A reachable catalog that does not list the model: "missing"; unreachable ({}): "unknown".
    return "missing" if statuses else "unknown"


async def _statuses_async(settings: Settings) -> Any:
    try:
        return await asyncio.wait_for(asyncio.to_thread(catalog_statuses, settings), timeout=8)
    except (asyncio.TimeoutError, Exception):
        return None


@router.get("/system")
async def system(request: Request, user: dict = Depends(current_user)):
    base = _settings(request)
    settings = await asyncio.to_thread(db.effective_settings, base)
    statuses = await _statuses_async(settings)
    usage = shutil.disk_usage(settings.data_dir)
    with db.opened(settings.db_path) as conn:
        hb = conn.execute("SELECT * FROM worker_status WHERE id=1").fetchone()
        queued = conn.execute("SELECT count(*) FROM meetings WHERE status IN ('queued','running','paused')").fetchone()[0]
    worker = {"heartbeat_at": hb["heartbeat_at"] if hb else None, "meeting_id": None, "stage": None,
              "queue_length": queued}
    if hb and hb["meeting_id"]:
        with db.opened(settings.db_path) as conn:
            owner = conn.execute("SELECT owner_id FROM meetings WHERE id=?", (hb["meeting_id"],)).fetchone()
        if owner and owner["owner_id"] == user["id"]:
            worker.update(meeting_id=hb["meeting_id"], stage=hb["stage"])
    return {
        "stt": {"model": settings.stt_model, "status": status_of(statuses, settings.stt_model, "stt")},
        "vision": {"model": settings.vision_model, "status": status_of(statuses, settings.vision_model, "vision")},
        "text": {"model": settings.text_model, "status": status_of(statuses, settings.text_model, "text")},
        "disk": {"free_bytes": usage.free, "total_bytes": usage.total, "reserve_bytes": settings.disk_reserve_bytes},
        "max_upload_bytes": settings.max_upload_bytes,
        "worker": worker,
    }


def _stt_choices(base: Settings) -> list[str]:
    return list(dict.fromkeys([*STT_MODEL_CHOICES, base.stt_model]))


async def settings_payload(base: Settings) -> dict[str, Any]:
    eff = await asyncio.to_thread(db.effective_settings, base)
    statuses = await _statuses_async(eff)
    options = []
    for model in _stt_choices(base):
        st = status_of(statuses, model)
        options.append({"model": model, "status": st, "selectable": st in SELECTABLE_STATUSES})
    return {"stt_model": eff.stt_model, "text_model": eff.text_model, "vision_model": eff.vision_model,
            "video_retention_days": eff.video_retention_days, "stt_model_options": options}


@router.get("/settings")
async def get_settings_api(request: Request, user: dict = Depends(current_user)):
    return await settings_payload(_settings(request))


@router.patch("/settings")
async def patch_settings(request: Request, body: dict = Body(...), user: dict = Depends(require_admin)):
    base = _settings(request)
    unknown = set(body) - set(Settings.OVERRIDABLE)
    if unknown:
        raise HTTPException(400, f"Unknown settings: {', '.join(sorted(unknown))}.")
    values: dict[str, Any] = {}
    if "stt_model" in body:
        model = body["stt_model"]
        if model not in _stt_choices(base):
            raise HTTPException(400, "Unknown STT model.")
        st = status_of(await _statuses_async(base), model)
        if st not in SELECTABLE_STATUSES:
            raise HTTPException(409, f"{model} is {st} in llm-proxy; only ready or degraded models can be selected.")
        values["stt_model"] = model
    for key in ("text_model", "vision_model"):
        if key in body:
            v = body[key]
            if not isinstance(v, str) or not v.strip() or len(v) > 200:
                raise HTTPException(400, f"{key} must be a model id.")
            values[key] = v.strip()
    if "video_retention_days" in body:
        v = body["video_retention_days"]
        if not isinstance(v, int) or isinstance(v, bool) or v < -1 or v > 3650:
            raise HTTPException(400, "video_retention_days must be -1 (forever) or 0-3650.")
        values["video_retention_days"] = v
    with db.opened(base.db_path) as conn:
        db.save_overrides(conn, values)
    return await settings_payload(base)
