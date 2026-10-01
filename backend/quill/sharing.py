"""Meeting sharing (docs/ACCOUNTS.md).

A meeting belongs to whoever uploaded it. The owner can share it with teammates,
each with 'view' (watch, read, search, export) or 'edit' (also rename the meeting
and its speakers), and/or with everyone on this Quill. Only the owner can re-run
stages, delete the meeting or change who it is shared with. Admins get no
implicit access: meetings stay private unless shared.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from .auth import current_user, get_db
from .stages import now_iso

router = APIRouter(prefix="/api")

LEVELS = ("view", "edit")
RANK = {"view": 1, "edit": 2, "owner": 3}
GONE = ("deleting", "cancelled")

# Meetings a user can open: their own, shared with them, or shared with everyone.
# Two positional params: (user_id, user_id).
VISIBLE = ("(owner_id=? OR everyone_access IS NOT NULL"
           " OR id IN (SELECT meeting_id FROM meeting_shares WHERE user_id=?))")
VISIBLE_M = ("(m.owner_id=? OR m.everyone_access IS NOT NULL"
             " OR m.id IN (SELECT meeting_id FROM meeting_shares WHERE user_id=?))")


def access_of(row: sqlite3.Row, user_id: int, share: str | None) -> str | None:
    """'owner', 'edit', 'view' or None, from the meeting row and the user's share (if any)."""
    if row["owner_id"] == user_id:
        return "owner"
    levels = {a for a in (share, row["everyone_access"]) if a in LEVELS}
    if not levels:
        return None
    return "edit" if "edit" in levels else "view"


def meeting_access(conn: sqlite3.Connection, row: sqlite3.Row, user_id: int) -> str | None:
    share = conn.execute("SELECT access FROM meeting_shares WHERE meeting_id=? AND user_id=?",
                         (row["id"], user_id)).fetchone()
    return access_of(row, user_id, share[0] if share else None)


def my_shares(conn: sqlite3.Connection, user_id: int) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT meeting_id, access FROM meeting_shares WHERE user_id=?",
                                             (user_id,))}


def person(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"] or "", "email": row["email"]}


def people_by_id(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    return {r["id"]: {"id": r["id"], "name": r["name"] or "", "email": r["email"]}
            for r in conn.execute("SELECT id, name, email FROM users")}


def sharing_fields(conn: sqlite3.Connection, row: sqlite3.Row, access: str,
                   people: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """What the UI needs on a meeting: your access, the owner, and whether the owner shared it."""
    if people is None:
        owner = person(conn.execute("SELECT id, name, email FROM users WHERE id=?", (row["owner_id"],)).fetchone())
    else:
        owner = people.get(row["owner_id"])
    out: dict[str, Any] = {"access": access, "owner": owner, "everyone_access": row["everyone_access"]}
    if access == "owner":
        out["shared_with"] = conn.execute("SELECT count(*) FROM meeting_shares WHERE meeting_id=?",
                                          (row["id"],)).fetchone()[0]
    return out


def _owned(conn: sqlite3.Connection, meeting_id: str, user: dict) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if row is None or row["status"] in GONE:
        raise HTTPException(404, "Meeting not found.")
    access = meeting_access(conn, row, user["id"])
    if access is None:
        raise HTTPException(404, "Meeting not found.")
    if access != "owner":
        raise HTTPException(403, "Only the person who uploaded this meeting can change who it's shared with.")
    return row


def sharing_state(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT s.user_id, s.access, s.created_at, u.name, u.email, u.disabled_at FROM meeting_shares s"
        " JOIN users u ON u.id=s.user_id WHERE s.meeting_id=? ORDER BY lower(coalesce(nullif(u.name, ''), u.email))",
        (row["id"],)).fetchall()
    owner = person(conn.execute("SELECT id, name, email FROM users WHERE id=?", (row["owner_id"],)).fetchone())
    return {"owner": owner, "everyone": row["everyone_access"],
            "people": [{"user_id": r["user_id"], "name": r["name"] or "", "email": r["email"], "access": r["access"],
                        "shared_at": r["created_at"], "disabled": bool(r["disabled_at"])} for r in rows]}


@router.get("/directory")
def directory(user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    """Everyone you could share with: active accounts, by name."""
    rows = conn.execute("SELECT id, name, email FROM users WHERE disabled_at IS NULL"
                        " ORDER BY lower(coalesce(nullif(name, ''), email))").fetchall()
    return [person(r) for r in rows]


@router.get("/meetings/{meeting_id}/sharing")
def get_sharing(meeting_id: str, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    return sharing_state(conn, _owned(conn, meeting_id, user))


@router.patch("/meetings/{meeting_id}/sharing")
def update_sharing(meeting_id: str, body: dict = Body(...), user: dict = Depends(current_user),
                   conn: sqlite3.Connection = Depends(get_db)):
    """{everyone?: 'view'|'edit'|null, people?: {"<user id>": 'view'|'edit'|null}} - null removes."""
    row = _owned(conn, meeting_id, user)
    people = body.get("people") or {}
    if not isinstance(people, dict):
        raise HTTPException(400, "people must be an object of {user id: access}.")
    changes: list[tuple[int, str | None]] = []
    for key, access in people.items():
        try:
            uid = int(key)
        except (TypeError, ValueError):
            raise HTTPException(400, "people keys must be user ids.") from None
        if access is not None and access not in LEVELS:
            raise HTTPException(400, "Access must be 'view', 'edit' or null.")
        if uid == row["owner_id"]:
            raise HTTPException(400, "The owner always has access.")
        target = conn.execute("SELECT disabled_at FROM users WHERE id=?", (uid,)).fetchone()
        if access is not None and (target is None or target["disabled_at"]):
            raise HTTPException(400, "You can only share with active accounts on this Quill.")
        changes.append((uid, access))
    everyone_set = "everyone" in body
    everyone = body.get("everyone")
    if everyone_set and everyone is not None and everyone not in LEVELS:
        raise HTTPException(400, "everyone must be 'view', 'edit' or null.")
    now = now_iso()
    with conn:
        if everyone_set:
            conn.execute("UPDATE meetings SET everyone_access=? WHERE id=?", (everyone, meeting_id))
        for uid, access in changes:
            if access is None:
                conn.execute("DELETE FROM meeting_shares WHERE meeting_id=? AND user_id=?", (meeting_id, uid))
            else:
                conn.execute(
                    "INSERT INTO meeting_shares(meeting_id, user_id, access, shared_by, created_at) VALUES(?,?,?,?,?)"
                    " ON CONFLICT(meeting_id, user_id) DO UPDATE SET access=excluded.access",
                    (meeting_id, uid, access, user["id"], now))
    return sharing_state(conn, conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone())
