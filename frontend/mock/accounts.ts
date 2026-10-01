// Accounts for the dev mock (docs/ACCOUNTS.md): users, sessions with device info,
// invite links (optional email, role, expiry, renew, withdraw) and password resets.
// Mirrors quill/auth.py + quill/users.py closely enough for UI work. Tokens are kept
// in plain text here; the real API stores only digests.

import type { IncomingMessage, ServerResponse } from "node:http";
import { randomBytes } from "node:crypto";

export interface MUser {
  id: number;
  email: string;
  password: string;
  name: string;
  is_admin: boolean;
  created_at: string;
  disabled_at: string | null;
  last_seen_at: string | null;
  invited_by: number | null;
  meetings: number;
  bytes: number;
}

interface MSession {
  id: string;
  token: string;
  user_id: number;
  created_at: string;
  last_seen_at: string;
  user_agent: string;
  ip: string;
}

interface MInvite {
  id: string;
  token: string;
  email: string | null;
  name: string;
  is_admin: boolean;
  created_by: number | null;
  created_at: string;
  expires_at: string;
  used_at: string | null;
  used_by: number | null;
  revoked_at: string | null;
}

interface Helpers {
  send: (res: ServerResponse, status: number, body?: unknown, headers?: Record<string, string>) => void;
  err: (res: ServerResponse, status: number, msg: string) => void;
  readJson: (req: IncomingMessage) => Promise<Record<string, unknown>>;
  cookie: (req: IncomingMessage, name: string) => string | null;
}

export const MOCK_EMAIL = "admin@quill.test";
export const MOCK_PASSWORD = "quill-dev";

const ago = (h: number) => new Date(Date.now() - h * 3600_000).toISOString();
const ahead = (h: number) => new Date(Date.now() + h * 3600_000).toISOString();
const now = () => new Date().toISOString();
const hex = (n: number) => randomBytes(n).toString("hex");
const tok = () => randomBytes(18).toString("base64url");
const norm = (e: unknown) => String(e ?? "").trim().toLowerCase();
const validEmail = (e: string) => e.length >= 3 && e.includes("@") && !e.includes(" ");
const cleanName = (n: unknown) => String(n ?? "").replace(/\s+/g, " ").trim().slice(0, 80);
const day = (iso: string) => new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });

const UA_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15";
const UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 Version/19.0 Mobile/15E148 Safari/604.1";
const UA_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0";

export function mockAccounts({ send, err, readJson, cookie }: Helpers) {
  const empty = !!process.env.QUILL_MOCK_SETUP;
  const user = (id: number, email: string, name: string, extra: Partial<MUser> = {}): MUser => ({
    id,
    email,
    name,
    password: id === 1 ? MOCK_PASSWORD : "member-pass",
    is_admin: false,
    created_at: ago(24 * (40 - id * 5)),
    disabled_at: null,
    last_seen_at: null,
    invited_by: 1,
    meetings: 0,
    bytes: 0,
    ...extra,
  });
  const users: MUser[] = empty
    ? []
    : [
        user(1, MOCK_EMAIL, "Alex Morgan", { is_admin: true, invited_by: null, meetings: 6, bytes: 3.4e9 }),
        user(2, "priya@quill.test", "Priya Raman", { last_seen_at: ago(2), meetings: 4, bytes: 2.1e9 }),
        user(3, "tomas@quill.test", "Tomás Ortega", { is_admin: true, last_seen_at: ago(26), meetings: 2, bytes: 8.6e8 }),
        user(4, "mei@quill.test", "Mei Chen", { disabled_at: ago(72), last_seen_at: ago(240), meetings: 1, bytes: 2.4e8 }),
        user(5, "daniel@quill.test", "", { invited_by: 3 }),
      ];
  const sessions: MSession[] = empty
    ? []
    : [
        { id: hex(8), token: tok(), user_id: 1, created_at: ago(30), last_seen_at: ago(3), user_agent: UA_IPHONE, ip: "192.168.1.41" },
        { id: hex(8), token: tok(), user_id: 1, created_at: ago(24 * 9), last_seen_at: ago(24 * 5), user_agent: UA_LINUX, ip: "203.0.113.7" },
        { id: hex(8), token: tok(), user_id: 2, created_at: ago(5), last_seen_at: ago(2), user_agent: UA_MAC, ip: "192.168.1.52" },
      ];
  const invites: MInvite[] = empty
    ? []
    : [
        { id: hex(8), token: tok(), email: "sam@quill.test", name: "Sam Okafor", is_admin: false, created_by: 1, created_at: ago(20), expires_at: ahead(24 * 6 - 20), used_at: null, used_by: null, revoked_at: null },
        { id: hex(8), token: tok(), email: null, name: "", is_admin: false, created_by: 3, created_at: ago(24 * 9), expires_at: ago(48), used_at: null, used_by: null, revoked_at: null },
        { id: hex(8), token: tok(), email: "priya@quill.test", name: "Priya", is_admin: false, created_by: 1, created_at: ago(24 * 20), expires_at: ago(24 * 13), used_at: ago(24 * 19), used_by: 2, revoked_at: null },
      ];
  const resets: { token: string; user_id: number; expires_at: string; used_at: string | null }[] = [];

  const byId = (id: number | null) => users.find((u) => u.id === id);
  const who = (u: MUser | undefined) => (u ? u.name || u.email : null);
  const publicUser = (u: MUser) => ({ id: u.id, email: u.email, name: u.name, is_admin: u.is_admin, role: u.is_admin ? "admin" : "member", created_at: u.created_at });
  const status = (i: MInvite) => (i.used_at ? "used" : i.revoked_at ? "revoked" : i.expires_at <= now() ? "expired" : "pending");
  const inviteOut = (i: MInvite) => ({
    id: i.id,
    email: i.email,
    name: i.name,
    is_admin: i.is_admin,
    role: i.is_admin ? "admin" : "member",
    created_at: i.created_at,
    expires_at: i.expires_at,
    used_at: i.used_at,
    revoked_at: i.revoked_at,
    status: status(i),
    invited_by: who(byId(i.created_by)),
    used_by: byId(i.used_by)?.email ?? null,
  });
  const person = (u: MUser) => ({
    ...publicUser(u),
    disabled: !!u.disabled_at,
    disabled_at: u.disabled_at,
    last_seen_at: u.last_seen_at,
    invited_by: who(byId(u.invited_by)),
    meeting_count: u.meetings,
    uploaded_bytes: u.bytes,
    session_count: sessions.filter((s) => s.user_id === u.id).length,
  });
  const otherAdmins = (id: number) => users.filter((u) => u.is_admin && !u.disabled_at && u.id !== id).length;
  const LAST_ADMIN = "Quill needs at least one admin. Make someone else an admin first.";
  const origin = (req: IncomingMessage) => `http://${req.headers.host}`;

  function sessionOf(req: IncomingMessage) {
    const t = cookie(req, "quill_session");
    return t ? sessions.find((s) => s.token === t) : undefined;
  }
  function currentUser(req: IncomingMessage): MUser | null {
    const s = sessionOf(req);
    const u = s && byId(s.user_id);
    if (!s || !u || u.disabled_at) return null;
    s.last_seen_at = now();
    u.last_seen_at = now();
    return u;
  }
  function signIn(req: IncomingMessage, res: ServerResponse, u: MUser) {
    const s: MSession = { id: hex(8), token: tok(), user_id: u.id, created_at: now(), last_seen_at: now(), user_agent: String(req.headers["user-agent"] ?? ""), ip: "127.0.0.1" };
    sessions.push(s);
    u.last_seen_at = now();
    send(res, 200, { user: publicUser(u) }, { "Set-Cookie": `quill_session=${s.token}; Path=/; HttpOnly; SameSite=Lax` });
  }
  const drop = (pred: (s: MSession) => boolean) => {
    let n = 0;
    for (let i = sessions.length - 1; i >= 0; i--) if (pred(sessions[i])) (sessions.splice(i, 1), n++);
    return n;
  };
  function findInvite(token: string): [number, string] | MInvite {
    const i = invites.find((x) => x.token === token);
    if (!i) return [404, "This invite link isn't valid. Check that you copied all of it."];
    const by = who(byId(i.created_by)) ?? "an admin";
    const st = status(i);
    if (st === "used") return [410, "This invite has already been used. Sign in instead."];
    if (st === "revoked") return [410, `This invite was withdrawn. Ask ${by} for a new link.`];
    if (st === "expired") return [410, `This invite expired on ${day(i.expires_at)}. Ask ${by} for a new link.`];
    return i;
  }
  function findReset(token: string): [number, string] | (typeof resets)[number] {
    const r = resets.find((x) => x.token === token);
    if (!r) return [404, "This reset link isn't valid. Check that you copied all of it."];
    if (r.used_at) return [410, "This reset link has already been used. Ask an admin for a new one."];
    if (r.expires_at <= now()) return [410, "This reset link has expired. Ask an admin for a new one."];
    return r;
  }

  /** Routes that work signed out. Returns true when handled. */
  async function handlePublic(req: IncomingMessage, res: ServerResponse, p: string, method: string, url: URL): Promise<boolean> {
    if (p === "/api/auth/state") return (send(res, 200, { needs_setup: users.length === 0 }), true);
    if (p === "/api/auth/setup" && method === "POST") {
      if (users.length) return (err(res, 409, "Setup is already complete."), true);
      const b = await readJson(req);
      if (!validEmail(norm(b.email))) return (err(res, 400, "Enter a valid email address."), true);
      if (String(b.password ?? "").length < 8) return (err(res, 400, "Password must be at least 8 characters."), true);
      const u = user(1, norm(b.email), cleanName(b.name), { password: String(b.password), is_admin: true, invited_by: null, created_at: now() });
      users.push(u);
      return (signIn(req, res, u), true);
    }
    if (p === "/api/auth/login" && method === "POST") {
      const b = await readJson(req);
      const u = users.find((x) => x.email === norm(b.email) && x.password === b.password);
      if (!u) return (err(res, 401, "Email or password is not correct."), true);
      if (u.disabled_at) return (err(res, 403, "This account is turned off. Ask an admin of this Quill to turn it back on."), true);
      return (signIn(req, res, u), true);
    }
    if (p === "/api/auth/logout" && method === "POST") {
      const s = sessionOf(req);
      if (s) drop((x) => x === s);
      return (send(res, 200, { ok: true }, { "Set-Cookie": "quill_session=; Path=/; Max-Age=0" }), true);
    }
    if (p === "/api/auth/invite" && method === "GET") {
      const i = findInvite(url.searchParams.get("token") ?? "");
      if (Array.isArray(i)) return (err(res, i[0], i[1]), true);
      send(res, 200, { email: i.email, name: i.name, is_admin: i.is_admin, role: i.is_admin ? "admin" : "member", invited_by: who(byId(i.created_by)) ?? "an admin", expires_at: i.expires_at });
      return true;
    }
    if (p === "/api/auth/accept" && method === "POST") {
      const b = await readJson(req);
      const i = findInvite(String(b.token ?? ""));
      if (Array.isArray(i)) return (err(res, 403, i[1]), true);
      const email = i.email ?? norm(b.email);
      if (!validEmail(email)) return (err(res, 400, "Enter a valid email address."), true);
      if (String(b.password ?? "").length < 8) return (err(res, 400, "Password must be at least 8 characters."), true);
      if (users.some((u) => u.email === email)) return (err(res, 409, "That email already has an account. Sign in instead."), true);
      const u = user(Math.max(0, ...users.map((x) => x.id)) + 1, email, cleanName(b.name) || i.name, {
        password: String(b.password),
        is_admin: i.is_admin,
        invited_by: i.created_by,
        created_at: now(),
      });
      users.push(u);
      i.used_at = now();
      i.used_by = u.id;
      return (signIn(req, res, u), true);
    }
    if (p === "/api/auth/reset" && method === "GET") {
      const r = findReset(url.searchParams.get("token") ?? "");
      if (Array.isArray(r)) return (err(res, r[0], r[1]), true);
      const u = byId(r.user_id)!;
      return (send(res, 200, { email: u.email, name: u.name, expires_at: r.expires_at }), true);
    }
    if (p === "/api/auth/reset" && method === "POST") {
      const b = await readJson(req);
      const r = findReset(String(b.token ?? ""));
      if (Array.isArray(r)) return (err(res, r[0], r[1]), true);
      if (String(b.password ?? "").length < 8) return (err(res, 400, "Password must be at least 8 characters."), true);
      const u = byId(r.user_id)!;
      u.password = String(b.password);
      r.used_at = now();
      drop((s) => s.user_id === u.id);
      return (signIn(req, res, u), true);
    }
    return false;
  }

  /** Signed-in routes: your account, people and invites. Returns true when handled. */
  async function handleAuthed(req: IncomingMessage, res: ServerResponse, p: string, method: string, url: URL, me: MUser): Promise<boolean> {
    const mine = sessionOf(req);
    if (p === "/api/auth/me") return (send(res, 200, { user: publicUser(me) }), true);
    if (p === "/api/account" && method === "PATCH") {
      const b = await readJson(req);
      if (typeof b.email === "string" && norm(b.email) !== me.email) {
        if (!validEmail(norm(b.email))) return (err(res, 400, "Enter a valid email address."), true);
        if (b.current_password !== me.password) return (err(res, 400, "Your current password is not correct."), true);
        if (users.some((u) => u.email === norm(b.email) && u.id !== me.id)) return (err(res, 409, "Someone else already uses that email."), true);
        me.email = norm(b.email);
      }
      if (typeof b.name === "string") me.name = cleanName(b.name);
      return (send(res, 200, { user: publicUser(me) }), true);
    }
    if (p === "/api/account/password" && method === "POST") {
      const b = await readJson(req);
      if (b.current_password !== me.password) return (err(res, 400, "Your current password is not correct."), true);
      if (String(b.new_password ?? "").length < 8) return (err(res, 400, "Password must be at least 8 characters."), true);
      me.password = String(b.new_password);
      return (send(res, 200, { ok: true, signed_out: drop((s) => s.user_id === me.id && s !== mine) }), true);
    }
    if (p === "/api/account/sessions" && method === "GET") {
      const list = sessions
        .filter((s) => s.user_id === me.id)
        .sort((a, b) => (a === mine ? -1 : b === mine ? 1 : b.last_seen_at.localeCompare(a.last_seen_at)))
        .map((s) => ({ id: s.id, created_at: s.created_at, last_seen_at: s.last_seen_at, user_agent: s.user_agent, ip: s.ip, current: s === mine }));
      return (send(res, 200, list), true);
    }
    if (p === "/api/account/sessions/revoke-others" && method === "POST") {
      return (send(res, 200, { signed_out: drop((s) => s.user_id === me.id && s !== mine) }), true);
    }
    let m = /^\/api\/account\/sessions\/([^/]+)$/.exec(p);
    if (m && method === "DELETE") {
      const s = sessions.find((x) => x.id === m![1] && x.user_id === me.id);
      if (!s) return (err(res, 404, "That device is already signed out."), true);
      drop((x) => x === s);
      return (send(res, 200, { ok: true, current: s === mine }, s === mine ? { "Set-Cookie": "quill_session=; Path=/; Max-Age=0" } : {}), true);
    }

    const adminOnly = p === "/api/users" || p.startsWith("/api/users/") || p === "/api/invites" || p.startsWith("/api/invites/");
    if (adminOnly && !me.is_admin) return (err(res, 403, "Only an administrator can do that."), true);

    if (p === "/api/users" && method === "GET") {
      const list = [...users].sort((a, b) => Number(b.is_admin) - Number(a.is_admin) || (a.name || a.email).localeCompare(b.name || b.email));
      return (send(res, 200, list.map(person)), true);
    }
    m = /^\/api\/users\/(\d+)(?:\/(reset-link|sign-out))?$/.exec(p);
    if (m) {
      const t = byId(Number(m[1]));
      if (!t) return (err(res, 404, "No such person."), true);
      if (!m[2] && method === "PATCH") {
        const b = await readJson(req);
        const activeAdmin = t.is_admin && !t.disabled_at;
        if (b.disabled === true && t.id === me.id) return (err(res, 400, "You can't turn off your own account."), true);
        if (activeAdmin && (b.is_admin === false || b.disabled === true) && !otherAdmins(t.id)) return (err(res, 409, LAST_ADMIN), true);
        if (typeof b.is_admin === "boolean") t.is_admin = b.is_admin;
        if (typeof b.disabled === "boolean") {
          t.disabled_at = b.disabled ? now() : null;
          if (b.disabled) drop((s) => s.user_id === t.id);
        }
        return (send(res, 200, person(t)), true);
      }
      if (!m[2] && method === "DELETE") {
        const what = url.searchParams.get("meetings") ?? "transfer";
        if (t.id === me.id) return (err(res, 400, "You can't remove your own account."), true);
        if (t.is_admin && !t.disabled_at && !otherAdmins(t.id)) return (err(res, 409, LAST_ADMIN), true);
        const n = t.meetings;
        if (what === "transfer") (me.meetings += n), (me.bytes += t.bytes);
        users.splice(users.indexOf(t), 1);
        drop((s) => s.user_id === t.id);
        return (send(res, 200, { ok: true, meetings: n, action: what }), true);
      }
      if (m[2] === "reset-link" && method === "POST") {
        if (t.disabled_at) return (err(res, 409, "Turn the account back on first."), true);
        for (let i = resets.length - 1; i >= 0; i--) if (resets[i].user_id === t.id && !resets[i].used_at) resets.splice(i, 1);
        const r = { token: tok(), user_id: t.id, expires_at: ahead(24), used_at: null };
        resets.push(r);
        return (send(res, 200, { url: `${origin(req)}/reset/${r.token}`, expires_at: r.expires_at, email: t.email, name: t.name }), true);
      }
      if (m[2] === "sign-out" && method === "POST") return (send(res, 200, { signed_out: drop((s) => s.user_id === t.id) }), true);
    }

    if (p === "/api/invites" && method === "GET") {
      const list = [...invites].sort((a, b) => b.created_at.localeCompare(a.created_at)).map(inviteOut);
      return (send(res, 200, list), true);
    }
    if (p === "/api/invites" && method === "POST") {
      const b = await readJson(req);
      const email = norm(b.email) || null;
      if (email && !validEmail(email)) return (err(res, 400, "Enter a valid email address, or leave it empty for an open link."), true);
      if (email && users.some((u) => u.email === email)) return (err(res, 409, "That email already has an account."), true);
      const days = Number(b.days ?? 7);
      if (![1, 7, 30].includes(days)) return (err(res, 400, "Invites can last 1, 7 or 30 days."), true);
      if (email) for (const i of invites) if (i.email === email && !i.used_at && !i.revoked_at) i.revoked_at = now();
      const i: MInvite = { id: hex(8), token: tok(), email, name: cleanName(b.name), is_admin: !!b.is_admin, created_by: me.id, created_at: now(), expires_at: ahead(days * 24), used_at: null, used_by: null, revoked_at: null };
      invites.push(i);
      return (send(res, 200, { ...inviteOut(i), url: `${origin(req)}/invite/${i.token}` }), true);
    }
    m = /^\/api\/invites\/([^/]+)(?:\/(renew))?$/.exec(p);
    if (m) {
      const i = invites.find((x) => x.id === m![1]);
      if (!i) return (err(res, 404, "No such invite."), true);
      if (i.used_at) return (err(res, 409, "That invite was already used."), true);
      if (m[2] === "renew" && method === "POST") {
        const b = await readJson(req);
        i.token = tok();
        i.expires_at = ahead(Number(b.days ?? 7) * 24);
        i.revoked_at = null;
        i.created_by = me.id;
        return (send(res, 200, { ...inviteOut(i), url: `${origin(req)}/invite/${i.token}` }), true);
      }
      if (!m[2] && method === "DELETE") {
        i.revoked_at ??= now();
        return (send(res, 200, inviteOut(i)), true);
      }
    }
    return false;
  }

  const brief = (u: MUser) => ({ id: u.id, name: u.name, email: u.email });
  return {
    currentUser,
    handlePublic,
    handleAuthed,
    /** For sharing: {id, name, email} of anyone, and the active people one can share with. */
    person: (id: number) => {
      const u = byId(id);
      return u ? brief(u) : null;
    },
    directory: () => users.filter((u) => !u.disabled_at).sort((a, b) => (a.name || a.email).localeCompare(b.name || b.email)).map(brief),
    isActive: (id: number) => !!byId(id) && !byId(id)!.disabled_at,
  };
}
