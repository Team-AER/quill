"""Authentication, modeled on Erised: first-run admin setup, invited users,
scrypt password hashes, and an HttpOnly SameSite=Lax session cookie
(Secure when the request came over https). Login attempts are rate limited in memory.

Quill sends no email: invites and password resets are one-time links an admin
copies and sends (docs/ACCOUNTS.md). Only keyed digests of every token are stored.
This module holds the primitives and the public routes; quill.users has the
signed-in account routes and the admin people/invite routes.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import hmac
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from . import db
from .config import Settings
from .stages import now_iso

COOKIE = "quill_session"
SESSION_DAYS = 30
INVITE_DAYS = 7
RESET_HOURS = 24
MIN_PASSWORD = 8
MAX_NAME = 80
SEEN_EVERY_S = 300  # write last_seen_at at most this often per session

SCRYPT_N = 2 ** 15


# ------------------------------------------------------------ passwords

def hash_password(password: str, salt: str | None = None, n: int = SCRYPT_N) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=n, r=8, p=1,
                            maxmem=128 * 1024 * 1024).hex()
    return f"scrypt${n}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, salt, _ = encoded.split("$")
        if scheme != "scrypt":
            return False
        return hmac.compare_digest(hash_password(password, salt, int(n)), encoded)
    except (ValueError, TypeError, AttributeError):
        return False


_DUMMY_HASH = hash_password("not-a-real-password", "00" * 16)


def token_digest(settings: Settings, token: str) -> str:
    key = settings.secret_key.encode()
    if key:
        return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()
    return hashlib.sha256(token.encode()).hexdigest()


def iso_in(**delta: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat(timespec="seconds")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return 3 <= len(email) <= 254 and "@" in email and " " not in email


def clean_name(name: str | None) -> str:
    """Collapse whitespace, drop control characters, cap the length."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", name or "")
    return re.sub(r"\s+", " ", name).strip()[:MAX_NAME]


def check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > 1024:
        raise HTTPException(400, "Password is too long.")


def display_date(iso: str | None) -> str:
    try:
        d = datetime.fromisoformat(iso or "")
    except ValueError:
        return ""
    return f"{d.day} {d:%b %Y}"


# ------------------------------------------------------------ rate limiting

class RateLimiter:
    """Sliding-window counter per key (in-process; the API is a single process)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > 10000:  # bound memory
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


LOGIN_WINDOW = 600.0
LOGIN_PER_IP = 30
LOGIN_PER_EMAIL = 10
PREVIEW_PER_IP = 120


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit(request: Request, key: str, count: int, message: str = "Too many attempts. Please wait a few minutes.") -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    if not limiter.allow(key, count, LOGIN_WINDOW):
        raise HTTPException(429, message)


def _rate_limit(request: Request, email: str) -> None:
    msg = "Too many sign-in attempts. Please wait a few minutes."
    limit(request, "ip:" + client_ip(request), LOGIN_PER_IP, msg)
    limit(request, "email:" + email, LOGIN_PER_EMAIL, msg)


# ------------------------------------------------------------ dependencies

def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request):
    conn = db.connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def public_user(row: sqlite3.Row) -> dict[str, Any]:
    is_admin = bool(row["is_admin"])
    return {"id": row["id"], "email": row["email"], "name": row["name"] or "", "is_admin": is_admin,
            "role": "admin" if is_admin else "member", "created_at": row["created_at"]}


def session_digest(request: Request) -> str | None:
    token = request.cookies.get(COOKIE, "")
    return token_digest(request.app.state.settings, token) if token else None


def user_from_request(request: Request) -> dict[str, Any] | None:
    digest = session_digest(request)
    if not digest:
        return None
    settings: Settings = request.app.state.settings
    now = now_iso()
    with db.opened(settings.db_path) as conn:
        row = conn.execute(
            "SELECT u.*, s.last_seen_at AS s_seen FROM users u JOIN sessions s ON s.user_id=u.id"
            " WHERE s.token=? AND s.expires_at>? AND u.disabled_at IS NULL", (digest, now)).fetchone()
        if row is None:
            return None
        if (row["s_seen"] or "") < iso_in(seconds=-SEEN_EVERY_S):
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE token=?", (now, digest))
            conn.execute("UPDATE users SET last_seen_at=? WHERE id=?", (now, row["id"]))
    return public_user(row)


def current_user(request: Request) -> dict[str, Any]:
    user = user_from_request(request)
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict[str, Any]:
    if not user["is_admin"]:
        raise HTTPException(403, "Only an administrator can do that.")
    return user


def base_url(request: Request | None, settings: Settings) -> str:
    if settings.public_url:
        return settings.public_url
    return str(request.base_url).rstrip("/") if request is not None else ""


def start_session(request: Request, response: Response, user_id: int) -> None:
    settings: Settings = request.app.state.settings
    token = secrets.token_urlsafe(32)
    now = now_iso()
    with db.opened(settings.db_path) as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        conn.execute(
            "INSERT INTO sessions(token, user_id, expires_at, id, created_at, last_seen_at, user_agent, ip)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (token_digest(settings, token), user_id, iso_in(days=SESSION_DAYS), secrets.token_hex(8), now, now,
             request.headers.get("user-agent", "")[:300], client_ip(request)))
        conn.execute("UPDATE users SET last_seen_at=? WHERE id=?", (now, user_id))
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, path="/", httponly=True,
                        samesite="lax", secure=_secure_request(request))


def clear_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=_secure_request(request))


def _secure_request(request: Request) -> bool:
    # Secure only over https (behind the TLS proxy); LAN users on a plain
    # http:// address would otherwise never get the cookie back.
    return request.url.scheme == "https"


def _is_local_client(request: Request) -> bool:
    host = client_ip(request)
    if host == "testclient":  # Starlette's TestClient
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def who(name: str | None, email: str | None) -> str:
    return (name or "").strip() or (email or "") or "an admin"


# ------------------------------------------------------------ invite + reset lookups

def invite_status(row: sqlite3.Row, now: str | None = None) -> str:
    if row["used_at"]:
        return "used"
    if row["revoked_at"]:
        return "revoked"
    if row["expires_at"] <= (now or now_iso()):
        return "expired"
    return "pending"


def _find_invite(conn: sqlite3.Connection, digest: str) -> sqlite3.Row:
    """The pending invite for a token digest, or an HTTPException that says what is wrong with it."""
    inv = conn.execute(
        "SELECT i.*, c.name AS by_name, c.email AS by_email FROM invites i"
        " LEFT JOIN users c ON c.id=i.created_by WHERE i.token=?", (digest,)).fetchone()
    if inv is None:
        raise HTTPException(404, "This invite link isn't valid. Check that you copied all of it.")
    status = invite_status(inv)
    inviter = who(inv["by_name"], inv["by_email"])
    if status == "used":
        raise HTTPException(410, "This invite has already been used. Sign in instead.")
    if status == "revoked":
        raise HTTPException(410, f"This invite was withdrawn. Ask {inviter} for a new link.")
    if status == "expired":
        raise HTTPException(410, f"This invite expired on {display_date(inv['expires_at'])}. Ask {inviter} for a new link.")
    return inv


def _find_reset(conn: sqlite3.Connection, digest: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT r.*, u.email, u.name, u.disabled_at FROM password_resets r JOIN users u ON u.id=r.user_id"
        " WHERE r.token=?", (digest,)).fetchone()
    if row is None:
        raise HTTPException(404, "This reset link isn't valid. Check that you copied all of it.")
    if row["used_at"]:
        raise HTTPException(410, "This reset link has already been used. Ask an admin for a new one.")
    if row["expires_at"] <= now_iso():
        raise HTTPException(410, "This reset link has expired. Ask an admin for a new one.")
    if row["disabled_at"]:
        raise HTTPException(410, "This account is turned off. Ask an admin to turn it back on.")
    return row


def _preview_limit(request: Request) -> None:
    limit(request, "preview:" + client_ip(request), PREVIEW_PER_IP)


# ------------------------------------------------------------ routes

class Credentials(BaseModel):
    email: str
    password: str


class SetupBody(BaseModel):
    email: str
    password: str
    name: str = ""


class AcceptInvite(BaseModel):
    token: str
    password: str
    name: str = ""
    email: str = ""


class ResetBody(BaseModel):
    token: str
    password: str


router = APIRouter()


@router.get("/api/auth/state")
def auth_state(conn: sqlite3.Connection = Depends(get_db)):
    count = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    return {"needs_setup": count == 0}


@router.post("/api/auth/setup")
async def setup(body: SetupBody, request: Request, response: Response):
    # Until an admin exists anyone reaching setup could claim it, so only the LAN may.
    if not _is_local_client(request):
        raise HTTPException(403, "First-run setup is only allowed from the local network.")
    email = normalize_email(body.email)
    if not valid_email(email):
        raise HTTPException(400, "Enter a valid email address.")
    check_password(body.password)
    settings: Settings = request.app.state.settings
    encoded = await asyncio.to_thread(hash_password, body.password)
    with db.opened(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT count(*) FROM users").fetchone()[0]:
            raise HTTPException(409, "Setup is already complete.")
        now = now_iso()
        cur = conn.execute(
            "INSERT INTO users(email, name, password_hash, is_admin, created_at, password_changed_at)"
            " VALUES(?,?,?,1,?,?)", (email, clean_name(body.name), encoded, now, now))
        row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    start_session(request, response, row["id"])
    return {"user": public_user(row)}


@router.post("/api/auth/login")
async def login(body: Credentials, request: Request, response: Response):
    email = normalize_email(body.email)
    _rate_limit(request, email)
    settings: Settings = request.app.state.settings
    with db.opened(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    encoded = row["password_hash"] if row else _DUMMY_HASH
    ok = await asyncio.to_thread(verify_password, body.password or "", encoded)
    if row is None or not ok:
        raise HTTPException(401, "Email or password is not correct.")
    if row["disabled_at"]:
        raise HTTPException(403, "This account is turned off. Ask an admin of this Quill to turn it back on.")
    start_session(request, response, row["id"])
    return {"user": public_user(row)}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    settings: Settings = request.app.state.settings
    digest = session_digest(request)
    if digest:
        with db.opened(settings.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (digest,))
    clear_cookie(request, response)
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    return {"user": user}


@router.get("/api/auth/invite")
def invite_preview(request: Request, token: str = Query(""), conn: sqlite3.Connection = Depends(get_db)):
    _preview_limit(request)
    inv = _find_invite(conn, token_digest(request.app.state.settings, token.strip()))
    return {"email": inv["email"], "name": inv["name"] or "", "is_admin": bool(inv["is_admin"]),
            "role": "admin" if inv["is_admin"] else "member", "invited_by": who(inv["by_name"], inv["by_email"]),
            "expires_at": inv["expires_at"]}


@router.post("/api/auth/accept")
async def accept_invite(body: AcceptInvite, request: Request, response: Response):
    settings: Settings = request.app.state.settings
    limit(request, "accept:" + client_ip(request), LOGIN_PER_IP)
    digest = token_digest(settings, (body.token or "").strip())

    def lookup(conn: sqlite3.Connection) -> sqlite3.Row:
        try:
            return _find_invite(conn, digest)
        except HTTPException as exc:  # one status for every dead link
            raise HTTPException(403, exc.detail) from None

    with db.opened(settings.db_path) as conn:
        inv = lookup(conn)
    email = inv["email"] or normalize_email(body.email)
    if not valid_email(email):
        raise HTTPException(400, "Enter a valid email address.")
    check_password(body.password)
    encoded = await asyncio.to_thread(hash_password, body.password)
    with db.opened(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        inv = lookup(conn)
        now = now_iso()
        try:
            cur = conn.execute(
                "INSERT INTO users(email, name, password_hash, is_admin, created_at, password_changed_at, invited_by)"
                " VALUES(?,?,?,?,?,?,?)",
                (email, clean_name(body.name) or inv["name"] or "", encoded, int(bool(inv["is_admin"])), now, now,
                 inv["created_by"]))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That email already has an account. Sign in instead.") from None
        conn.execute("UPDATE invites SET used_at=?, used_by=? WHERE token=?", (now, cur.lastrowid, digest))
        row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    start_session(request, response, row["id"])
    return {"user": public_user(row)}


@router.get("/api/auth/reset")
def reset_preview(request: Request, token: str = Query(""), conn: sqlite3.Connection = Depends(get_db)):
    _preview_limit(request)
    row = _find_reset(conn, token_digest(request.app.state.settings, token.strip()))
    return {"email": row["email"], "name": row["name"] or "", "expires_at": row["expires_at"]}


@router.post("/api/auth/reset")
async def reset_password(body: ResetBody, request: Request, response: Response):
    settings: Settings = request.app.state.settings
    limit(request, "reset:" + client_ip(request), LOGIN_PER_IP)
    digest = token_digest(settings, (body.token or "").strip())
    with db.opened(settings.db_path) as conn:
        _find_reset(conn, digest)
    check_password(body.password)
    encoded = await asyncio.to_thread(hash_password, body.password)
    with db.opened(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _find_reset(conn, digest)
        now = now_iso()
        conn.execute("UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?", (encoded, now, row["user_id"]))
        conn.execute("UPDATE password_resets SET used_at=? WHERE token=?", (now, digest))
        conn.execute("DELETE FROM password_resets WHERE user_id=? AND used_at IS NULL", (row["user_id"],))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
        user = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    start_session(request, response, user["id"])
    return {"user": public_user(user)}
