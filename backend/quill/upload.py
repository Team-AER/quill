"""tus 1.0.0 resumable upload server.

Extensions: creation, creation-with-upload, termination, checksum (sha256, sha1, md5).
PATCH bodies stream straight to disk. Upload state lives in the `uploads` table,
partial data in {DATA_DIR}/uploads/<upload_id>. When the last byte arrives the file
moves to media/<meeting_id>/source.<ext>, a queued meeting is created and the
response carries `Quill-Meeting-Id`.

Upload-Metadata keys: filename, title, language, expected_speakers, audio_only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.requests import ClientDisconnect

from . import db
from .auth import current_user
from .config import Settings
from .stages import now_iso

TUS_VERSION = "1.0.0"
TUS_EXTENSIONS = "creation,creation-with-upload,termination,checksum"
CHECKSUM_ALGORITHMS = {"sha256": hashlib.sha256, "sha1": hashlib.sha1, "md5": hashlib.md5}
MAX_OPEN_UPLOADS_PER_USER = 20
WRITE_BUFFER = 1024 * 1024

router = APIRouter()
_locks: dict[str, asyncio.Lock] = {}


class TusError(HTTPException):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(status, message, headers={"Tus-Resumable": TUS_VERSION, **(headers or {})})


def _tus_headers(**extra: Any) -> dict[str, str]:
    headers = {"Tus-Resumable": TUS_VERSION, "Cache-Control": "no-store"}
    headers.update({k.replace("_", "-"): str(v) for k, v in extra.items()})
    return headers


def _require_tus(request: Request) -> None:
    if request.headers.get("tus-resumable") != TUS_VERSION:
        raise TusError(412, "Unsupported tus version.", {"Tus-Version": TUS_VERSION})


# ------------------------------------------------------------ metadata

def parse_metadata(header: str | None) -> dict[str, str]:
    """Parse `key b64value,key2 b64value2,flag` into a dict of decoded strings."""
    out: dict[str, str] = {}
    if not header:
        return out
    for pair in header.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(" ")
        key = parts[0]
        if not key or key in out or len(parts) > 2:
            raise TusError(400, "Malformed Upload-Metadata.")
        if len(parts) == 1:
            out[key] = ""
            continue
        try:
            out[key] = base64.b64decode(parts[1], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise TusError(400, "Malformed Upload-Metadata.")
    return out


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def clean_metadata(raw: dict[str, str]) -> dict[str, Any]:
    filename = Path((raw.get("filename") or "").replace("\\", "/")).name.strip()[:255] or "recording"
    title = (raw.get("title") or "").strip()[:200] or (Path(filename).stem[:200] or "Untitled meeting")
    language = (raw.get("language") or "").strip()[:16] or None
    # "auto" (or anything that is not a language tag) means: no hint.
    if language and not re.fullmatch(r"[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})?", language):
        language = None
    if language and language.lower() == "auto":
        language = None
    speakers_raw = (raw.get("expected_speakers") or "").strip()
    expected = None
    if speakers_raw:
        try:
            expected = int(speakers_raw)
        except ValueError:
            raise TusError(400, "expected_speakers must be a number.")
        if not 1 <= expected <= 8:
            raise TusError(400, "expected_speakers must be between 1 and 8.")
    return {"filename": filename, "title": title, "language": language,
            "expected_speakers": expected, "audio_only": _truthy(raw.get("audio_only"))}


def source_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    return ext if re.fullmatch(r"[a-z0-9]{1,8}", ext) else "bin"


# ------------------------------------------------------------ helpers

def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _partial_path(settings: Settings, upload_id: str) -> Path:
    return settings.uploads_dir / upload_id


def _load(conn: sqlite3.Connection, upload_id: str, user: dict) -> sqlite3.Row:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise TusError(404, "Upload not found.")
    row = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
    if row is None or row["owner_id"] != user["id"]:
        raise TusError(404, "Upload not found.")
    return row


def reserved_bytes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT coalesce(sum(length - received), 0) FROM uploads WHERE completed_at IS NULL").fetchone()
    return int(row[0] or 0)


def check_disk(settings: Settings, conn: sqlite3.Connection, length: int) -> None:
    free = shutil.disk_usage(settings.uploads_dir).free
    if free - reserved_bytes(conn) < length + settings.disk_reserve_bytes:
        raise TusError(507, "Not enough free disk space for this upload right now.")


def _parse_checksum(header: str | None):
    if not header:
        return None
    try:
        algo, b64 = header.strip().split(" ", 1)
        expected = base64.b64decode(b64.strip(), validate=True)
    except (ValueError, binascii.Error):
        raise TusError(400, "Malformed Upload-Checksum.")
    factory = CHECKSUM_ALGORITHMS.get(algo.lower())
    if factory is None:
        raise TusError(400, "Unsupported checksum algorithm.")
    return factory(), expected


async def _receive(request: Request, settings: Settings, upload: sqlite3.Row) -> tuple[int, dict[str, str]]:
    """Stream the request body onto the partial file. Returns (new_offset, extra headers)."""
    path = _partial_path(settings, upload["id"])
    offset = upload["received"]
    length = upload["length"]
    checksum = _parse_checksum(request.headers.get("upload-checksum"))
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if offset + int(content_length) > length:
                raise TusError(413, "Upload exceeds the declared Upload-Length.")
        except ValueError:
            raise TusError(400, "Bad Content-Length.")

    written = 0
    overflow = False
    disconnected = False
    fh = await asyncio.to_thread(open, path, "r+b")
    try:
        await asyncio.to_thread(fh.seek, offset)
        buf = bytearray()
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if offset + written + len(buf) + len(chunk) > length:
                    overflow = True
                    break
                if checksum:
                    checksum[0].update(chunk)
                buf += chunk
                if len(buf) >= WRITE_BUFFER:
                    data, buf = bytes(buf), bytearray()
                    await asyncio.to_thread(fh.write, data)
                    written += len(data)
        except ClientDisconnect:
            disconnected = True
        if buf and not overflow:
            await asyncio.to_thread(fh.write, bytes(buf))
            written += len(buf)
        await asyncio.to_thread(fh.flush)
        await asyncio.to_thread(os.fsync, fh.fileno())
    finally:
        await asyncio.to_thread(fh.close)

    def rollback():
        with open(path, "r+b") as f:
            f.truncate(offset)

    if overflow:
        await asyncio.to_thread(rollback)
        raise TusError(413, "Upload exceeds the declared Upload-Length.")
    if checksum is not None:
        if disconnected or checksum[0].digest() != checksum[1]:
            await asyncio.to_thread(rollback)
            if disconnected:
                raise TusError(400, "Client disconnected before the chunk completed.")
            raise TusError(460, "Checksum mismatch.")
    new_offset = offset + written
    with db.opened(settings.db_path) as conn:
        conn.execute("UPDATE uploads SET received=? WHERE id=?", (new_offset, upload["id"]))
    extra: dict[str, str] = {}
    if new_offset == length:
        meeting_id = await asyncio.to_thread(finalize_upload, settings, upload["id"])
        extra["Quill-Meeting-Id"] = meeting_id
    return new_offset, extra


def finalize_upload(settings: Settings, upload_id: str) -> str:
    """Move the completed upload into media/<meeting_id>/ and create the meeting."""
    with db.opened(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        if row["meeting_id"]:
            return row["meeting_id"]
        meta = json.loads(row["metadata"] or "{}")
        meeting_id = db.new_id()
        media_dir = settings.media_dir(meeting_id)
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / f"source.{source_extension(meta.get('filename', ''))}"
        os.replace(_partial_path(settings, upload_id), target)
        db.create_meeting(
            conn, meeting_id=meeting_id, owner_id=row["owner_id"], title=meta.get("title") or "Untitled meeting",
            mode="audio" if meta.get("audio_only") else "video", language=meta.get("language"),
            expected_speakers=meta.get("expected_speakers"), source_name=meta.get("filename"),
            source_bytes=row["length"], source_path=str(target))
        conn.execute("UPDATE uploads SET completed_at=?, meeting_id=? WHERE id=?",
                     (now_iso(), meeting_id, upload_id))
        return meeting_id


# ------------------------------------------------------------ routes

@router.options("/api/uploads")
@router.options("/api/uploads/{upload_id}")
def tus_options(request: Request):
    settings = _settings(request)
    return Response(status_code=204, headers=_tus_headers(
        Tus_Version=TUS_VERSION, Tus_Extension=TUS_EXTENSIONS, Tus_Max_Size=settings.max_upload_bytes,
        Tus_Checksum_Algorithm=",".join(CHECKSUM_ALGORITHMS)))


@router.post("/api/uploads")
async def tus_create(request: Request, user: dict = Depends(current_user)):
    _require_tus(request)
    settings = _settings(request)
    if request.headers.get("upload-defer-length"):
        raise TusError(400, "Upload-Defer-Length is not supported.")
    try:
        length = int(request.headers.get("upload-length", ""))
    except ValueError:
        raise TusError(400, "Upload-Length is required.")
    if length <= 0:
        raise TusError(400, "Upload-Length must be positive.")
    if length > settings.max_upload_bytes:
        raise TusError(413, "The file is larger than the upload limit.",
                       {"Tus-Max-Size": str(settings.max_upload_bytes)})
    raw_meta = request.headers.get("upload-metadata", "")
    meta = clean_metadata(parse_metadata(raw_meta))

    upload_id = db.new_id()
    with db.opened(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        open_count = conn.execute("SELECT count(*) FROM uploads WHERE owner_id=? AND completed_at IS NULL",
                                  (user["id"],)).fetchone()[0]
        if open_count >= MAX_OPEN_UPLOADS_PER_USER:
            raise TusError(429, "Too many unfinished uploads. Finish or cancel some first.")
        check_disk(settings, conn, length)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        _partial_path(settings, upload_id).touch()
        conn.execute("INSERT INTO uploads(id, owner_id, length, received, metadata, raw_metadata, created_at)"
                     " VALUES(?,?,?,0,?,?,?)",
                     (upload_id, user["id"], length, json.dumps(meta), raw_meta[:4096], now_iso()))

    headers = _tus_headers(Location=f"/api/uploads/{upload_id}")
    if request.headers.get("content-type", "").split(";")[0].strip() == "application/offset+octet-stream":
        with db.opened(settings.db_path) as conn:
            row = conn.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        lock = _locks.setdefault(upload_id, asyncio.Lock())
        async with lock:
            offset, extra = await _receive(request, settings, row)
        headers.update(extra)
        headers["Upload-Offset"] = str(offset)
    return Response(status_code=201, headers=headers)


@router.head("/api/uploads/{upload_id}")
def tus_head(upload_id: str, request: Request, user: dict = Depends(current_user)):
    _require_tus(request)
    settings = _settings(request)
    with db.opened(settings.db_path) as conn:
        row = _load(conn, upload_id, user)
    headers = _tus_headers(Upload_Offset=row["received"], Upload_Length=row["length"])
    if row["raw_metadata"]:
        headers["Upload-Metadata"] = row["raw_metadata"]
    if row["meeting_id"]:
        headers["Quill-Meeting-Id"] = row["meeting_id"]
    return Response(status_code=200, headers=headers)


@router.patch("/api/uploads/{upload_id}")
async def tus_patch(upload_id: str, request: Request, user: dict = Depends(current_user)):
    _require_tus(request)
    settings = _settings(request)
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/offset+octet-stream":
        raise TusError(415, "Content-Type must be application/offset+octet-stream.")
    try:
        client_offset = int(request.headers.get("upload-offset", ""))
    except ValueError:
        raise TusError(400, "Upload-Offset is required.")
    lock = _locks.setdefault(upload_id, asyncio.Lock())
    if lock.locked():
        raise TusError(409, "Another request is writing to this upload.")
    async with lock:
        with db.opened(settings.db_path) as conn:
            row = _load(conn, upload_id, user)
        if row["completed_at"]:
            if client_offset == row["length"]:
                return Response(status_code=204, headers=_tus_headers(
                    Upload_Offset=row["length"], Quill_Meeting_Id=row["meeting_id"] or ""))
            raise TusError(409, "Upload is already complete.")
        if client_offset != row["received"]:
            raise TusError(409, "Upload-Offset does not match.", {"Upload-Offset": str(row["received"])})
        offset, extra = await _receive(request, settings, row)
    headers = _tus_headers(Upload_Offset=offset)
    headers.update(extra)
    return Response(status_code=204, headers=headers)


@router.delete("/api/uploads/{upload_id}")
def tus_delete(upload_id: str, request: Request, user: dict = Depends(current_user)):
    _require_tus(request)
    settings = _settings(request)
    with db.opened(settings.db_path) as conn:
        row = _load(conn, upload_id, user)
        conn.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
    if not row["completed_at"]:
        _partial_path(settings, upload_id).unlink(missing_ok=True)
    _locks.pop(upload_id, None)
    return Response(status_code=204, headers=_tus_headers())


def sweep_stale_uploads(settings: Settings, max_age_days: int = 7) -> int:
    """Drop unfinished uploads older than max_age_days. Returns how many were removed."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    removed = 0
    with db.opened(settings.db_path) as conn:
        rows = conn.execute("SELECT id FROM uploads WHERE completed_at IS NULL AND created_at<?", (cutoff,)).fetchall()
        for row in rows:
            _partial_path(settings, row["id"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM uploads WHERE id=?", (row["id"],))
            removed += 1
        # Completed upload bookkeeping is only needed briefly (HEAD after finish).
        conn.execute("DELETE FROM uploads WHERE completed_at IS NOT NULL AND completed_at<?", (cutoff,))
    return removed
