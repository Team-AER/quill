import sqlite3

import pytest

from quill import db
from quill.config import Settings
from quill.stages import STAGES


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "q.sqlite3"
    db.init(path)
    c = db.connect(path)
    yield c
    c.close()


def test_init_is_idempotent_and_versioned(tmp_path):
    path = tmp_path / "sub" / "q.sqlite3"
    db.init(path)
    db.init(path)
    c = db.connect(path)
    assert db.schema_version(c) == db.MIGRATIONS[-1][0]
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("users", "sessions", "invites", "meetings", "stages", "speakers", "turns", "segments",
              "transcript_lines", "moments", "frames", "notes", "events", "settings", "uploads",
              "transcript_fts", "frame_fts", "schema_version"):
        assert t in tables


def test_migration_applies_new_versions(tmp_path, monkeypatch):
    path = tmp_path / "q.sqlite3"
    db.init(path)
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [(99, "CREATE TABLE extra(x INTEGER);")])
    db.init(path)
    c = db.connect(path)
    assert db.schema_version(c) == 99
    c.execute("INSERT INTO extra VALUES(1)")


def test_create_meeting_and_delete(conn):
    mid = db.create_meeting(conn, owner_id=1, title="Standup", mode="audio")
    conn.commit()
    rows = conn.execute("SELECT name, status FROM stages WHERE meeting_id=?", (mid,)).fetchall()
    assert sorted(r["name"] for r in rows) == sorted(STAGES)
    assert {r["status"] for r in rows} == {"pending"}
    conn.execute("INSERT INTO turns(meeting_id, speaker, start, end) VALUES(?,?,?,?)", (mid, "S1", 0, 1))
    db.delete_meeting_rows(conn, mid)
    conn.commit()
    assert conn.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM meetings").fetchone()[0] == 0


def test_fts_triggers(conn):
    conn.execute("INSERT INTO transcript_lines(id, meeting_id, speaker, start, end, text) VALUES(1,'m','S1',0,1,'Quarterly budget review')")
    conn.execute("INSERT INTO frames(id, meeting_id, t, visible_text, caption) VALUES(5,'m',3,'Revenue 2026','A chart')")
    hit = conn.execute("SELECT line_id FROM transcript_fts WHERE transcript_fts MATCH 'budg*'").fetchone()
    assert hit[0] == 1
    conn.execute("UPDATE transcript_lines SET text='nothing here' WHERE id=1")
    assert conn.execute("SELECT count(*) FROM transcript_fts WHERE transcript_fts MATCH 'budget'").fetchone()[0] == 0
    conn.execute("DELETE FROM transcript_lines WHERE id=1")
    assert conn.execute("SELECT count(*) FROM transcript_fts").fetchone()[0] == 0
    assert conn.execute("SELECT frame_id FROM frame_fts WHERE frame_fts MATCH 'revenue'").fetchone()[0] == 5
    conn.execute("DELETE FROM frames WHERE id=5")
    assert conn.execute("SELECT count(*) FROM frame_fts").fetchone()[0] == 0


def test_settings_overrides(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.ensure_dirs()
    db.init(s.db_path)
    with db.opened(s.db_path) as c:
        db.save_overrides(c, {"stt_model": "aer-stt-qwen3", "video_retention_days": 0})
    eff = db.effective_settings(s)
    assert eff.stt_model == "aer-stt-qwen3" and eff.video_retention_days == 0
    with db.opened(s.db_path) as c:
        db.save_overrides(c, {"stt_model": None})
    assert db.effective_settings(s).stt_model == "aer-stt-v1"


def test_auth_foreign_keys(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES('t', 42, '2099')")
