"""SQLite schema, connection helpers and a tiny migration mechanism.

    from quill import db
    db.init(path)            # create/upgrade schema (idempotent, safe across processes)
    conn = db.connect(path)  # WAL, foreign keys, Row factory; caller closes

Only auth tables declare foreign keys. Meeting child tables are keyed by
meeting_id without FK constraints so pipeline code (and its tests) can write rows
independently; `delete_meeting_rows` removes them explicitly.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .stages import STAGES, now_iso

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions(
    token TEXT PRIMARY KEY,                -- sha256/HMAC digest of the cookie value
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS invites(
    token TEXT PRIMARY KEY,                -- digest of the invite token
    email TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT);

CREATE TABLE IF NOT EXISTS meetings(
    id TEXT PRIMARY KEY,
    owner_id INTEGER,
    title TEXT,
    created_at TEXT,
    mode TEXT,
    language TEXT,
    expected_speakers INTEGER,
    duration_s REAL,
    source_name TEXT,
    source_bytes INTEGER,
    source_path TEXT,
    has_video INTEGER,
    width INTEGER,
    height INTEGER,
    status TEXT,
    error TEXT,
    source_deleted_at TEXT);
CREATE INDEX IF NOT EXISTS meetings_owner ON meetings(owner_id, created_at);
CREATE INDEX IF NOT EXISTS meetings_status ON meetings(status);

CREATE TABLE IF NOT EXISTS stages(
    meeting_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    progress REAL NOT NULL DEFAULT 0,
    detail TEXT,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    options TEXT,                          -- addition: JSON rerun options
    PRIMARY KEY(meeting_id, name));

CREATE TABLE IF NOT EXISTS speakers(
    meeting_id TEXT NOT NULL,
    label TEXT NOT NULL,
    display_name TEXT,
    suggested_name TEXT,
    suggestion_evidence_t REAL,
    color INTEGER,
    PRIMARY KEY(meeting_id, label));

CREATE TABLE IF NOT EXISTS turns(
    id INTEGER PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    speaker TEXT,
    start REAL,
    end REAL,
    overlap INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS turns_meeting ON turns(meeting_id, start);

CREATE TABLE IF NOT EXISTS segments(
    id INTEGER PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    idx INTEGER,
    speaker TEXT,
    start REAL,
    end REAL,
    clip_sha256 TEXT,
    idempotency_key TEXT,
    stt_job_id TEXT,
    status TEXT,
    text TEXT,
    engine_model TEXT,
    error TEXT,
    interjections TEXT);
CREATE INDEX IF NOT EXISTS segments_meeting ON segments(meeting_id, idx);

CREATE TABLE IF NOT EXISTS transcript_lines(
    id INTEGER PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    speaker TEXT,
    start REAL,
    end REAL,
    text TEXT,
    overlap INTEGER DEFAULT 0,
    segment_id INTEGER);
CREATE INDEX IF NOT EXISTS transcript_meeting ON transcript_lines(meeting_id, start);

CREATE TABLE IF NOT EXISTS moments(
    id INTEGER PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    t REAL,
    source TEXT,
    why TEXT,
    look_for TEXT,
    phash TEXT);
CREATE INDEX IF NOT EXISTS moments_meeting ON moments(meeting_id, t);

CREATE TABLE IF NOT EXISTS frames(
    id INTEGER PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    moment_id INTEGER,
    t REAL,
    path TEXT,
    thumb_path TEXT,
    kind TEXT,
    title TEXT,
    visible_text TEXT,
    key_facts TEXT,
    relevance INTEGER,
    caption TEXT);
CREATE INDEX IF NOT EXISTS frames_meeting ON frames(meeting_id, t);

CREATE TABLE IF NOT EXISTS notes(
    meeting_id TEXT PRIMARY KEY,
    version INTEGER,
    json TEXT,
    model TEXT,
    created_at TEXT);

CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY,
    meeting_id TEXT,
    at TEXT,
    kind TEXT,
    message TEXT);
CREATE INDEX IF NOT EXISTS events_meeting ON events(meeting_id, id);

-- Additions (not in the contract schema) -------------------------------
CREATE TABLE IF NOT EXISTS settings(      -- admin overrides, JSON values
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT);

CREATE TABLE IF NOT EXISTS uploads(       -- tus upload state
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    length INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,   -- bytes on disk (tus Upload-Offset)
    metadata TEXT,                         -- JSON of decoded Upload-Metadata
    raw_metadata TEXT,                     -- header as sent (echoed on HEAD)
    created_at TEXT NOT NULL,
    completed_at TEXT,
    meeting_id TEXT);
CREATE INDEX IF NOT EXISTS uploads_owner ON uploads(owner_id);

CREATE TABLE IF NOT EXISTS worker_status(  -- single-row heartbeat
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat_at TEXT,
    meeting_id TEXT,
    stage TEXT,
    pid INTEGER);

-- Full-text search, kept in sync by triggers so pipeline code need not care.
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
    text, meeting_id UNINDEXED, line_id UNINDEXED, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS frame_fts USING fts5(
    visible_text, caption, title, meeting_id UNINDEXED, frame_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2');

CREATE TRIGGER IF NOT EXISTS transcript_lines_ai AFTER INSERT ON transcript_lines BEGIN
    INSERT INTO transcript_fts(rowid, text, meeting_id, line_id)
    VALUES (new.id, coalesce(new.text, ''), new.meeting_id, new.id);
END;
CREATE TRIGGER IF NOT EXISTS transcript_lines_ad AFTER DELETE ON transcript_lines BEGIN
    DELETE FROM transcript_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS transcript_lines_au AFTER UPDATE ON transcript_lines BEGIN
    DELETE FROM transcript_fts WHERE rowid = old.id;
    INSERT INTO transcript_fts(rowid, text, meeting_id, line_id)
    VALUES (new.id, coalesce(new.text, ''), new.meeting_id, new.id);
END;

CREATE TRIGGER IF NOT EXISTS frames_ai AFTER INSERT ON frames BEGIN
    INSERT INTO frame_fts(rowid, visible_text, caption, title, meeting_id, frame_id)
    VALUES (new.id, coalesce(new.visible_text, ''), coalesce(new.caption, ''),
            coalesce(new.title, ''), new.meeting_id, new.id);
END;
CREATE TRIGGER IF NOT EXISTS frames_ad AFTER DELETE ON frames BEGIN
    DELETE FROM frame_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS frames_au AFTER UPDATE ON frames BEGIN
    DELETE FROM frame_fts WHERE rowid = old.id;
    INSERT INTO frame_fts(rowid, visible_text, caption, title, meeting_id, frame_id)
    VALUES (new.id, coalesce(new.visible_text, ''), coalesce(new.caption, ''),
            coalesce(new.title, ''), new.meeting_id, new.id);
END;
"""

# Accounts (docs/ACCOUNTS.md): profile + status on users, device info on sessions,
# invites with public ids, optional email, role and withdrawal, and password resets.
SCHEMA_V2 = """
ALTER TABLE users ADD COLUMN name TEXT;
ALTER TABLE users ADD COLUMN disabled_at TEXT;
ALTER TABLE users ADD COLUMN last_seen_at TEXT;
ALTER TABLE users ADD COLUMN password_changed_at TEXT;
ALTER TABLE users ADD COLUMN invited_by INTEGER;

ALTER TABLE sessions ADD COLUMN id TEXT;
ALTER TABLE sessions ADD COLUMN created_at TEXT;
ALTER TABLE sessions ADD COLUMN last_seen_at TEXT;
ALTER TABLE sessions ADD COLUMN user_agent TEXT;
ALTER TABLE sessions ADD COLUMN ip TEXT;
UPDATE sessions SET id = lower(hex(randomblob(8))) WHERE id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sessions_id ON sessions(id);

CREATE TABLE invites_v2(
    id TEXT PRIMARY KEY,                   -- public id (list, renew, withdraw)
    token TEXT UNIQUE NOT NULL,            -- digest of the invite token
    email TEXT,                            -- NULL: anyone with the link, once
    name TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    revoked_at TEXT);
INSERT INTO invites_v2(id, token, email, created_by, expires_at, used_at)
    SELECT lower(hex(randomblob(8))), token, email, created_by, expires_at, used_at FROM invites;
DROP TABLE invites;
ALTER TABLE invites_v2 RENAME TO invites;

CREATE TABLE IF NOT EXISTS password_resets(
    token TEXT PRIMARY KEY,                -- digest of the reset token
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT);
CREATE INDEX IF NOT EXISTS password_resets_user ON password_resets(user_id);
"""

# Sharing (docs/ACCOUNTS.md): a meeting can be shared with teammates ('view' or 'edit')
# and/or with everyone on this Quill (meetings.everyone_access).
SCHEMA_V3 = """
ALTER TABLE meetings ADD COLUMN everyone_access TEXT;   -- NULL | 'view' | 'edit'
CREATE TABLE IF NOT EXISTS meeting_shares(
    meeting_id TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    access TEXT NOT NULL,                  -- 'view' | 'edit'
    shared_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT,
    PRIMARY KEY(meeting_id, user_id));
CREATE INDEX IF NOT EXISTS meeting_shares_user ON meeting_shares(user_id);
"""

# (version, script). Append new entries; never edit an applied one.
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
    (3, SCHEMA_V3),
]

MEETING_CHILD_TABLES = ("stages", "speakers", "turns", "segments", "transcript_lines",
                        "moments", "frames", "notes", "events", "meeting_shares")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def opened(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Connection that commits on success, rolls back on error and always closes."""
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def schema_version(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL)")
    row = conn.execute("SELECT max(version) AS v FROM schema_version").fetchone()
    return int(row["v"] or 0)


def init(path: str | Path) -> None:
    """Create or upgrade the schema. Serialised across processes with a file lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            conn = connect(path)
            try:
                current = schema_version(conn)
                conn.commit()
                for version, script in MIGRATIONS:
                    if version <= current:
                        continue
                    conn.executescript(
                        "BEGIN;\n" + script +
                        f"\nDELETE FROM schema_version;\nINSERT INTO schema_version VALUES({version});\nCOMMIT;")
            finally:
                conn.close()
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# ---------------------------------------------------------------- helpers

def new_id() -> str:
    return uuid.uuid4().hex


def create_meeting(conn: sqlite3.Connection, *, owner_id: int | None, title: str,
                   mode: str = "video", language: str | None = None,
                   expected_speakers: int | None = None, source_name: str | None = None,
                   source_bytes: int | None = None, source_path: str | None = None,
                   meeting_id: str | None = None, status: str = "queued") -> str:
    """Insert a meeting plus one pending row per stage. Caller commits."""
    meeting_id = meeting_id or new_id()
    conn.execute(
        "INSERT INTO meetings(id, owner_id, title, created_at, mode, language, expected_speakers,"
        " source_name, source_bytes, source_path, status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (meeting_id, owner_id, title, now_iso(), mode, language, expected_speakers,
         source_name, source_bytes, source_path, status))
    conn.executemany("INSERT INTO stages(meeting_id, name, status, progress) VALUES(?,?,'pending',0)",
                     [(meeting_id, name) for name in STAGES])
    add_event(conn, meeting_id, "created", "meeting created")
    return meeting_id


def add_event(conn: sqlite3.Connection, meeting_id: str | None, kind: str, message: str) -> None:
    conn.execute("INSERT INTO events(meeting_id, at, kind, message) VALUES(?,?,?,?)",
                 (meeting_id, now_iso(), kind, message[:2000]))


def delete_meeting_rows(conn: sqlite3.Connection, meeting_id: str) -> None:
    for table in MEETING_CHILD_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE meeting_id=?", (meeting_id,))
    conn.execute("UPDATE uploads SET meeting_id=NULL WHERE meeting_id=?", (meeting_id,))
    conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))


def load_overrides(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            out[row["key"]] = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
    return out


def save_overrides(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if value is None:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), now_iso()))


def effective_settings(settings: Settings, conn: sqlite3.Connection | None = None) -> Settings:
    """Settings with admin overrides from the settings table applied."""
    if conn is None:
        try:
            with opened(settings.db_path) as c:
                return settings.with_overrides(load_overrides(c))
        except sqlite3.Error:
            return settings
    return settings.with_overrides(load_overrides(conn))


def main(argv: list[str] | None = None) -> int:
    """`python -m quill.db migrate` - create/upgrade the schema under QUILL_DATA_DIR."""
    import sys
    args = sys.argv[1:] if argv is None else argv
    if args[:1] not in (["migrate"], []):
        print("usage: python -m quill.db migrate", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    settings.ensure_dirs()
    init(settings.db_path)
    conn = connect(settings.db_path)
    try:
        print(f"quill db {settings.db_path}: schema version {schema_version(conn)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
