"""People on this Quill: your own account (profile, password, devices), the admin
people list, invite links and password-reset links (docs/ACCOUNTS.md).

    python -m quill.users list | reset-link EMAIL | set-role EMAIL admin|member | enable EMAIL

is the shell fallback for a lone admin who is locked out (installed as `quill-users`).
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from . import db
from .auth import (INVITE_DAYS, LOGIN_PER_EMAIL, RESET_HOURS, base_url, check_password, clean_name, clear_cookie,
                   current_user, get_db, hash_password, invite_status, iso_in, limit, normalize_email, public_user,
                   require_admin, session_digest, token_digest, valid_email, verify_password, who)
from .config import Settings
from .stages import now_iso
from .worker import purge_meeting

router = APIRouter(prefix="/api")

LIVE = "status NOT IN ('deleting','cancelled')"
INVITE_DAY_CHOICES = (1, 7, 30)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _user_row(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such person.")
    return row


def other_active_admins(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("SELECT count(*) FROM users WHERE is_admin=1 AND disabled_at IS NULL AND id!=?",
                        (user_id,)).fetchone()[0]


LAST_ADMIN = "Quill needs at least one admin. Make someone else an admin first."


def issue_reset(conn: sqlite3.Connection, settings: Settings, user_id: int, created_by: int | None) -> tuple[str, str]:
    """A new reset token for user_id (earlier unused ones stop working). Caller commits."""
    token = secrets.token_urlsafe(24)
    expires = iso_in(hours=RESET_HOURS)
    conn.execute("DELETE FROM password_resets WHERE user_id=? AND used_at IS NULL", (user_id,))
    conn.execute("INSERT INTO password_resets(token, user_id, created_by, created_at, expires_at) VALUES(?,?,?,?,?)",
                 (token_digest(settings, token), user_id, created_by, now_iso(), expires))
    return token, expires


# ------------------------------------------------------------ your account

class ProfileBody(BaseModel):
    name: str | None = None
    email: str | None = None
    current_password: str = ""


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


async def _check_current(request: Request, conn: sqlite3.Connection, user_id: int, password: str) -> None:
    # 400, not 401: a 401 signs the browser out.
    limit(request, f"pw:{user_id}", LOGIN_PER_EMAIL)
    row = _user_row(conn, user_id)
    if not await asyncio.to_thread(verify_password, password or "", row["password_hash"]):
        raise HTTPException(400, "Your current password is not correct.")


@router.patch("/account")
async def update_account(body: ProfileBody, request: Request, user: dict = Depends(current_user),
                         conn: sqlite3.Connection = Depends(get_db)):
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = clean_name(body.name)
    if body.email is not None and normalize_email(body.email) != user["email"]:
        email = normalize_email(body.email)
        if not valid_email(email):
            raise HTTPException(400, "Enter a valid email address.")
        await _check_current(request, conn, user["id"], body.current_password)
        if conn.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (email, user["id"])).fetchone():
            raise HTTPException(409, "Someone else already uses that email.")
        fields["email"] = email
    if fields:
        with conn:
            conn.execute(f"UPDATE users SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                         (*fields.values(), user["id"]))
    return {"user": public_user(_user_row(conn, user["id"]))}


@router.post("/account/password")
async def change_password(body: PasswordBody, request: Request, user: dict = Depends(current_user),
                          conn: sqlite3.Connection = Depends(get_db)):
    await _check_current(request, conn, user["id"], body.current_password)
    check_password(body.new_password)
    encoded = await asyncio.to_thread(hash_password, body.new_password)
    with conn:
        conn.execute("UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
                     (encoded, now_iso(), user["id"]))
        n = conn.execute("DELETE FROM sessions WHERE user_id=? AND token!=?",
                         (user["id"], session_digest(request))).rowcount
    return {"ok": True, "signed_out": n}


@router.get("/account/sessions")
def list_sessions(request: Request, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    mine = session_digest(request)
    rows = conn.execute("SELECT * FROM sessions WHERE user_id=? AND expires_at>?"
                        " ORDER BY token=? DESC, coalesce(last_seen_at, created_at, '') DESC",
                        (user["id"], now_iso(), mine)).fetchall()
    return [{"id": r["id"], "created_at": r["created_at"], "last_seen_at": r["last_seen_at"],
             "user_agent": r["user_agent"] or "", "ip": r["ip"] or "", "current": r["token"] == mine} for r in rows]


@router.delete("/account/sessions/{session_id}")
def end_session(session_id: str, request: Request, response: Response, user: dict = Depends(current_user),
                conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT token FROM sessions WHERE id=? AND user_id=?", (session_id, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "That device is already signed out.")
    with conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    current = row["token"] == session_digest(request)
    if current:
        clear_cookie(request, response)
    return {"ok": True, "current": current}


@router.post("/account/sessions/revoke-others")
def end_other_sessions(request: Request, user: dict = Depends(current_user), conn: sqlite3.Connection = Depends(get_db)):
    with conn:
        n = conn.execute("DELETE FROM sessions WHERE user_id=? AND token!=?",
                         (user["id"], session_digest(request))).rowcount
    return {"signed_out": n}


# ------------------------------------------------------------ people (admin)

def _person(row: sqlite3.Row) -> dict[str, Any]:
    out = public_user(row)
    out.update({
        "disabled": bool(row["disabled_at"]),
        "disabled_at": row["disabled_at"],
        "last_seen_at": row["last_seen_at"],
        "invited_by": who(row["inv_name"], row["inv_email"]) if row["inv_email"] else None,
        "meeting_count": row["meeting_count"],
        "uploaded_bytes": row["uploaded_bytes"],
        "session_count": row["session_count"],
    })
    return out


PEOPLE_SQL = f"""
SELECT u.*, c.name AS inv_name, c.email AS inv_email,
  (SELECT count(*) FROM meetings m WHERE m.owner_id=u.id AND m.{LIVE}) AS meeting_count,
  (SELECT coalesce(sum(m.source_bytes), 0) FROM meetings m WHERE m.owner_id=u.id AND m.{LIVE}) AS uploaded_bytes,
  (SELECT count(*) FROM sessions s WHERE s.user_id=u.id AND s.expires_at>:now) AS session_count
FROM users u LEFT JOIN users c ON c.id=u.invited_by
"""


def _people(conn: sqlite3.Connection, user_id: int | None = None) -> list[dict[str, Any]]:
    where = " WHERE u.id=:id" if user_id is not None else ""
    rows = conn.execute(PEOPLE_SQL + where + " ORDER BY u.is_admin DESC, lower(coalesce(nullif(u.name, ''), u.email))",
                        {"now": now_iso(), "id": user_id}).fetchall()
    return [_person(r) for r in rows]


@router.get("/users")
def list_people(user: dict = Depends(require_admin), conn: sqlite3.Connection = Depends(get_db)):
    return _people(conn)


class PersonPatch(BaseModel):
    is_admin: bool | None = None
    disabled: bool | None = None


@router.patch("/users/{user_id}")
def update_person(user_id: int, body: PersonPatch, user: dict = Depends(require_admin),
                  conn: sqlite3.Connection = Depends(get_db)):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        target = _user_row(conn, user_id)
        active_admin = bool(target["is_admin"]) and not target["disabled_at"]
        if body.disabled and user_id == user["id"]:
            raise HTTPException(400, "You can't turn off your own account.")
        if active_admin and (body.is_admin is False or body.disabled) and not other_active_admins(conn, user_id):
            raise HTTPException(409, LAST_ADMIN)
        if body.is_admin is not None:
            conn.execute("UPDATE users SET is_admin=? WHERE id=?", (int(body.is_admin), user_id))
        if body.disabled is not None:
            conn.execute("UPDATE users SET disabled_at=? WHERE id=?", (now_iso() if body.disabled else None, user_id))
            if body.disabled:
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM password_resets WHERE user_id=? AND used_at IS NULL", (user_id,))
    return _people(conn, user_id)[0]


@router.post("/users/{user_id}/reset-link")
def reset_link(user_id: int, request: Request, user: dict = Depends(require_admin),
               conn: sqlite3.Connection = Depends(get_db)):
    target = _user_row(conn, user_id)
    if target["disabled_at"]:
        raise HTTPException(409, "Turn the account back on first.")
    with conn:
        token, expires = issue_reset(conn, _settings(request), user_id, user["id"])
    return {"url": f"{base_url(request, _settings(request))}/reset/{token}", "expires_at": expires,
            "email": target["email"], "name": target["name"] or ""}


@router.post("/users/{user_id}/sign-out")
def sign_out_person(user_id: int, user: dict = Depends(require_admin), conn: sqlite3.Connection = Depends(get_db)):
    _user_row(conn, user_id)
    with conn:
        n = conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount
    return {"signed_out": n}


@router.delete("/users/{user_id}")
def remove_person(user_id: int, request: Request, meetings: str = Query("transfer"),
                  user: dict = Depends(require_admin), conn: sqlite3.Connection = Depends(get_db)):
    if meetings not in ("transfer", "delete"):
        raise HTTPException(400, "meetings must be 'transfer' or 'delete'.")
    if user_id == user["id"]:
        raise HTTPException(400, "You can't remove your own account.")
    purge: list[str] = []
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        target = _user_row(conn, user_id)
        if target["is_admin"] and not target["disabled_at"] and not other_active_admins(conn, user_id):
            raise HTTPException(409, LAST_ADMIN)
        ids = [r[0] for r in conn.execute(f"SELECT id FROM meetings WHERE owner_id=? AND {LIVE}", (user_id,))]
        if meetings == "transfer":
            conn.execute("UPDATE meetings SET owner_id=? WHERE owner_id=?", (user["id"], user_id))
            conn.execute("DELETE FROM meeting_shares WHERE user_id=? AND meeting_id IN"
                         " (SELECT id FROM meetings WHERE owner_id=?)", (user["id"], user["id"]))
        else:
            for mid in ids:
                conn.execute("UPDATE meetings SET status='deleting' WHERE id=?", (mid,))
                if not conn.execute("SELECT 1 FROM stages WHERE meeting_id=? AND status='running'", (mid,)).fetchone():
                    purge.append(mid)
        conn.execute("UPDATE uploads SET owner_id=? WHERE owner_id=?", (user["id"], user_id))
        conn.execute("UPDATE users SET invited_by=NULL WHERE invited_by=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))  # sessions + resets cascade
    for mid in purge:  # running ones: the worker stops the stage and purges
        purge_meeting(_settings(request), mid)
    return {"ok": True, "meetings": len(ids), "action": meetings}


# ------------------------------------------------------------ invites (admin)

class InviteRequest(BaseModel):
    email: str = ""
    name: str = ""
    is_admin: bool = False
    days: int = INVITE_DAYS


def _invite_out(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "email": row["email"], "name": row["name"] or "", "is_admin": bool(row["is_admin"]),
            "role": "admin" if row["is_admin"] else "member", "created_at": row["created_at"],
            "expires_at": row["expires_at"], "used_at": row["used_at"], "revoked_at": row["revoked_at"],
            "status": invite_status(row), "invited_by": who(row["by_name"], row["by_email"]) if row["by_email"] else None,
            "used_by": row["used_email"]}


INVITES_SQL = """
SELECT i.*, c.name AS by_name, c.email AS by_email, u.email AS used_email
FROM invites i LEFT JOIN users c ON c.id=i.created_by LEFT JOIN users u ON u.id=i.used_by
"""


def _invite(conn: sqlite3.Connection, invite_id: str) -> sqlite3.Row:
    row = conn.execute(INVITES_SQL + " WHERE i.id=?", (invite_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such invite.")
    return row


def _invite_url(request: Request, token: str) -> str:
    return f"{base_url(request, _settings(request))}/invite/{token}"


def _check_days(days: int) -> int:
    if days not in INVITE_DAY_CHOICES:
        raise HTTPException(400, "Invites can last 1, 7 or 30 days.")
    return days


@router.get("/invites")
def list_invites(user: dict = Depends(require_admin), conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(INVITES_SQL + " ORDER BY coalesce(i.created_at, i.expires_at) DESC LIMIT 200").fetchall()
    return [_invite_out(r) for r in rows]


@router.post("/invites")
def create_invite(body: InviteRequest, request: Request, user: dict = Depends(require_admin),
                  conn: sqlite3.Connection = Depends(get_db)):
    email = normalize_email(body.email) or None
    if email is not None:
        if not valid_email(email):
            raise HTTPException(400, "Enter a valid email address, or leave it empty for an open link.")
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "That email already has an account.")
    days = _check_days(body.days)
    token = secrets.token_urlsafe(24)
    invite_id = secrets.token_hex(8)
    now = now_iso()
    with conn:
        if email is not None:  # re-inviting someone replaces their earlier link
            conn.execute("UPDATE invites SET revoked_at=? WHERE email=? AND used_at IS NULL AND revoked_at IS NULL",
                         (now, email))
        conn.execute("INSERT INTO invites(id, token, email, name, is_admin, created_by, created_at, expires_at)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     (invite_id, token_digest(_settings(request), token), email, clean_name(body.name),
                      int(body.is_admin), user["id"], now, iso_in(days=days)))
    return {**_invite_out(_invite(conn, invite_id)), "url": _invite_url(request, token)}


@router.post("/invites/{invite_id}/renew")
def renew_invite(invite_id: str, request: Request, body: dict = Body(default={}), user: dict = Depends(require_admin),
                 conn: sqlite3.Connection = Depends(get_db)):
    row = _invite(conn, invite_id)
    if row["used_at"]:
        raise HTTPException(409, "That invite was already used.")
    if row["email"] and conn.execute("SELECT 1 FROM users WHERE email=?", (row["email"],)).fetchone():
        raise HTTPException(409, "That email already has an account.")
    days = _check_days(int(body.get("days") or INVITE_DAYS))
    token = secrets.token_urlsafe(24)
    with conn:
        conn.execute("UPDATE invites SET token=?, expires_at=?, revoked_at=NULL, created_by=? WHERE id=?",
                     (token_digest(_settings(request), token), iso_in(days=days), user["id"], invite_id))
    return {**_invite_out(_invite(conn, invite_id)), "url": _invite_url(request, token)}


@router.delete("/invites/{invite_id}")
def withdraw_invite(invite_id: str, user: dict = Depends(require_admin), conn: sqlite3.Connection = Depends(get_db)):
    row = _invite(conn, invite_id)
    if row["used_at"]:
        raise HTTPException(409, "That invite was already used.")
    with conn:
        conn.execute("UPDATE invites SET revoked_at=coalesce(revoked_at, ?) WHERE id=?", (now_iso(), invite_id))
    return _invite_out(_invite(conn, invite_id))


# ------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="quill-users", description="Manage Quill accounts from a shell.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="everyone on this Quill")
    sub.add_parser("reset-link", help="print a 24-hour password reset link").add_argument("email")
    role = sub.add_parser("set-role", help="make someone an admin or a member")
    role.add_argument("email")
    role.add_argument("role", choices=["admin", "member"])
    sub.add_parser("enable", help="turn a turned-off account back on").add_argument("email")
    args = p.parse_args(argv)

    settings = Settings.from_env()
    db.init(settings.db_path)
    with db.opened(settings.db_path) as conn:
        if args.cmd == "list":
            for r in conn.execute("SELECT * FROM users ORDER BY id"):
                state = "off" if r["disabled_at"] else "active"
                role_s = "admin" if r["is_admin"] else "member"
                print(f"{r['id']:>4}  {r['email']:<36} {role_s:<7} {state:<7} {r['name'] or ''}")
            return 0
        row = conn.execute("SELECT * FROM users WHERE email=?", (normalize_email(args.email),)).fetchone()
        if row is None:
            print(f"No account for {args.email}.")
            return 1
        if args.cmd == "reset-link":
            token, expires = issue_reset(conn, settings, row["id"], None)
            url = f"{settings.public_url}/reset/{token}" if settings.public_url else f"/reset/{token} (on your Quill address)"
            print(f"Password reset link for {row['email']} (works once, until {expires}):\n{url}")
            if row["disabled_at"]:
                print("Note: this account is turned off. Run `enable` first.")
        elif args.cmd == "set-role":
            if args.role == "member" and row["is_admin"] and not other_active_admins(conn, row["id"]):
                print(LAST_ADMIN)
                return 1
            conn.execute("UPDATE users SET is_admin=? WHERE id=?", (int(args.role == "admin"), row["id"]))
            print(f"{row['email']} is now {'an admin' if args.role == 'admin' else 'a member'}.")
        elif args.cmd == "enable":
            conn.execute("UPDATE users SET disabled_at=NULL WHERE id=?", (row["id"],))
            print(f"{row['email']} can sign in again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
