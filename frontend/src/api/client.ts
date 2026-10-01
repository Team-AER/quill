import type {
  Frame,
  Invite,
  InvitePreview,
  Meeting,
  MeetingEvent,
  ModelInfo,
  Moment,
  Notes,
  Person,
  ResetPreview,
  SearchResults,
  Session,
  ShareLevel,
  Sharing,
  Settings,
  Speaker,
  Stage,
  SystemInfo,
  Teammate,
  TranscriptLine,
  User,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type Json = Record<string, unknown>;

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = undefined;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const o = (data && typeof data === "object" ? data : {}) as Json;
    const msg = String(o.error ?? o.detail ?? (typeof data === "string" && data.length < 200 ? data : "") ?? "") || res.statusText || `HTTP ${res.status}`;
    if (res.status === 401) window.dispatchEvent(new CustomEvent("quill:unauthorized"));
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

/** Accepts `[...]`, `{items:[...]}` or `{<key>:[...]}`. */
function unwrapList<T>(data: unknown, key: string): T[] {
  if (Array.isArray(data)) return data as T[];
  if (data && typeof data === "object") {
    const o = data as Json;
    if (Array.isArray(o[key])) return o[key] as T[];
    if (Array.isArray(o.items)) return o.items as T[];
  }
  return [];
}

function parseMaybeJson<T>(v: unknown, fallback: T): T {
  if (v == null) return fallback;
  if (typeof v === "string") {
    try {
      return JSON.parse(v) as T;
    } catch {
      return fallback;
    }
  }
  return v as T;
}

/** GET /meetings/{id} is "meeting + stages + speakers": accept flat or nested. */
export function normalizeMeeting(data: unknown): Meeting {
  const o = (data ?? {}) as Json;
  const base = (o.meeting && typeof o.meeting === "object" ? o.meeting : o) as Json;
  const m = { ...base } as unknown as Meeting;
  m.stages = unwrapList<Stage>(o.stages ?? base.stages, "stages");
  m.speakers = unwrapList<Speaker>(o.speakers ?? base.speakers, "speakers");
  return m;
}

function normalizeLine(l: TranscriptLine): TranscriptLine {
  return { ...l, interjections: parseMaybeJson(l.interjections, null) };
}

function normalizeFrame(f: Frame): Frame {
  return { ...f, key_facts: parseMaybeJson<string[]>(f.key_facts, []) ?? [] };
}

export function normalizeNotes(data: unknown): Notes | null {
  if (!data) return null;
  let o = data as Json;
  // API: {version, model, created_at, notes} with notes null until synthesis ran.
  if ("notes" in o) {
    if (!o.notes || typeof o.notes !== "object") return null;
    o = o.notes as Json;
  } else if (typeof o.json === "string" || (o.json && typeof o.json === "object")) o = parseMaybeJson<Json>(o.json, {});
  return {
    tldr: (o.tldr as string[]) ?? [],
    summary: (o.summary as string) ?? "",
    chapters: (o.chapters as Notes["chapters"]) ?? [],
    decisions: (o.decisions as Notes["decisions"]) ?? [],
    action_items: (o.action_items as Notes["action_items"]) ?? [],
    open_questions: (o.open_questions as Notes["open_questions"]) ?? [],
    key_visuals: (o.key_visuals as Notes["key_visuals"]) ?? [],
    speaker_suggestions: (o.speaker_suggestions as Notes["speaker_suggestions"]) ?? [],
  };
}

function normalizeModel(v: unknown): ModelInfo | null {
  if (!v) return null;
  if (typeof v === "string") return { model: v, status: "unknown" };
  const o = v as Json;
  return { model: String(o.model ?? o.id ?? "?"), status: String(o.status ?? "unknown") };
}

export function normalizeSystem(data: unknown): SystemInfo {
  const o = (data ?? {}) as Json;
  const disk = (o.disk ?? {}) as Json;
  return {
    stt: normalizeModel(o.stt),
    vision: normalizeModel(o.vision),
    text: normalizeModel(o.text),
    disk_free_bytes: (disk.free_bytes ?? o.disk_free_bytes ?? null) as number | null,
    disk_total_bytes: (disk.total_bytes ?? o.disk_total_bytes ?? null) as number | null,
    max_upload_bytes: (o.max_upload_bytes ?? null) as number | null,
    worker: (o.worker ?? null) as SystemInfo["worker"],
  };
}

function normalizeSettings(data: unknown): Settings {
  const o = (data ?? {}) as Json;
  const opts = unwrapList<Json>(o.stt_model_options, "options").map((x) => ({
    model: String(x.model),
    status: String(x.status ?? "unknown"),
    selectable: x.selectable != null ? Boolean(x.selectable) : x.status === "ready" || x.status === "degraded",
  }));
  return {
    stt_model: String(o.stt_model ?? ""),
    text_model: o.text_model as string | undefined,
    vision_model: o.vision_model as string | undefined,
    video_retention_days: typeof o.video_retention_days === "number" ? o.video_retention_days : -1,
    stt_model_options: opts,
  };
}

export function normalizeUser(data: unknown): User {
  const d = (data ?? {}) as Json;
  const u = (d.user ?? d) as Json;
  const is_admin = Boolean(u.is_admin);
  return {
    id: Number(u.id),
    email: String(u.email ?? ""),
    name: String(u.name ?? ""),
    is_admin,
    role: is_admin ? "admin" : "member",
    created_at: (u.created_at as string) ?? null,
  };
}

const q = (token: string) => `?token=${encodeURIComponent(token)}`;

export interface NewInvite {
  email?: string;
  name?: string;
  is_admin?: boolean;
  days?: number;
}

export const api = {
  authState: () => request<{ needs_setup: boolean }>("GET", "/auth/state"),
  me: async () => normalizeUser(await request<Json>("GET", "/auth/me")),
  login: (email: string, password: string) => request<unknown>("POST", "/auth/login", { email, password }),
  logout: () => request<unknown>("POST", "/auth/logout"),
  setup: (email: string, password: string, name: string) => request<unknown>("POST", "/auth/setup", { email, password, name }),
  invitePreview: (token: string) => request<InvitePreview>("GET", `/auth/invite${q(token)}`),
  accept: (token: string, body: { password: string; name?: string; email?: string }) =>
    request<unknown>("POST", "/auth/accept", { token, ...body }),
  resetPreview: (token: string) => request<ResetPreview>("GET", `/auth/reset${q(token)}`),
  reset: (token: string, password: string) => request<unknown>("POST", "/auth/reset", { token, password }),

  updateAccount: async (patch: { name?: string; email?: string; current_password?: string }) =>
    normalizeUser(await request<Json>("PATCH", "/account", patch)),
  changePassword: (current_password: string, new_password: string) =>
    request<{ signed_out: number }>("POST", "/account/password", { current_password, new_password }),
  sessions: async () => unwrapList<Session>(await request("GET", "/account/sessions"), "sessions"),
  endSession: (id: string) => request<{ current: boolean }>("DELETE", `/account/sessions/${encodeURIComponent(id)}`),
  endOtherSessions: () => request<{ signed_out: number }>("POST", "/account/sessions/revoke-others"),

  people: async () => unwrapList<Person>(await request("GET", "/users"), "users"),
  updatePerson: (id: number, patch: { is_admin?: boolean; disabled?: boolean }) => request<Person>("PATCH", `/users/${id}`, patch),
  resetLink: (id: number) => request<{ url: string; expires_at: string; email: string; name: string }>("POST", `/users/${id}/reset-link`),
  signOutPerson: (id: number) => request<{ signed_out: number }>("POST", `/users/${id}/sign-out`),
  removePerson: (id: number, meetings: "transfer" | "delete") =>
    request<{ meetings: number }>("DELETE", `/users/${id}?meetings=${meetings}`),
  invite: (body: NewInvite) => request<Invite & { url: string }>("POST", "/invites", body),
  renewInvite: (id: string, days?: number) => request<Invite & { url: string }>("POST", `/invites/${id}/renew`, days ? { days } : {}),
  withdrawInvite: (id: string) => request<Invite>("DELETE", `/invites/${id}`),

  meetings: async () => unwrapList<Meeting>(await request("GET", "/meetings"), "meetings").map(normalizeMeeting),
  meeting: async (id: string) => normalizeMeeting(await request("GET", `/meetings/${id}`)),
  renameMeeting: (id: string, title: string) => request<unknown>("PATCH", `/meetings/${id}`, { title }),
  deleteMeeting: (id: string) => request<unknown>("DELETE", `/meetings/${id}`),
  transcript: async (id: string) =>
    unwrapList<TranscriptLine>(await request("GET", `/meetings/${id}/transcript`), "lines").map(normalizeLine),
  notes: async (id: string) => {
    try {
      return normalizeNotes(await request("GET", `/meetings/${id}/notes`));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  },
  frames: async (id: string) =>
    unwrapList<Frame>(await request("GET", `/meetings/${id}/frames`), "frames").map(normalizeFrame),
  moments: async (id: string) => unwrapList<Moment>(await request("GET", `/meetings/${id}/moments`), "moments"),
  /** Returns the meeting's updated speakers list. */
  renameSpeaker: async (id: string, label: string, display_name: string | null) =>
    unwrapList<Speaker>(await request("PATCH", `/meetings/${id}/speakers/${label}`, { display_name }), "speakers"),
  mergeSpeakers: async (id: string, from: string, into: string) =>
    unwrapList<Speaker>(await request("POST", `/meetings/${id}/speakers/merge`, { from, into }), "speakers"),
  rerun: (id: string, stage: string, options: Record<string, unknown> = {}) =>
    request<unknown>("POST", `/meetings/${id}/rerun`, { stage, options }),

  search: async (q: string): Promise<SearchResults> => {
    const d = await request<Json>("GET", `/search?q=${encodeURIComponent(q)}`);
    return {
      transcript: unwrapList(d.transcript ?? d.lines, "hits"),
      frames: unwrapList(d.frames, "hits"),
    };
  },
  system: async () => normalizeSystem(await request("GET", "/system")),
  settings: async () => normalizeSettings(await request("GET", "/settings")),
  updateSettings: async (patch: { stt_model?: string; video_retention_days?: number }) =>
    normalizeSettings(await request("PATCH", "/settings", patch)),
  invites: async () => unwrapList<Invite>(await request("GET", "/invites"), "invites"),

  directory: async () => unwrapList<Teammate>(await request("GET", "/directory"), "people"),
  sharing: (id: string) => request<Sharing>("GET", `/meetings/${id}/sharing`),
  /** `people` maps user id to a level, or null to stop sharing with them. */
  updateSharing: (id: string, patch: { everyone?: ShareLevel | null; people?: Record<string, ShareLevel | null> }) =>
    request<Sharing>("PATCH", `/meetings/${id}/sharing`, patch),
};

export const mediaUrl = (id: string) => `/api/meetings/${id}/media`;
export const frameImageUrl = (frame: number | Pick<Frame, "id" | "image_url" | "thumb_url">, thumb = false) => {
  if (typeof frame === "object") {
    const u = thumb ? frame.thumb_url : frame.image_url;
    if (u) return u;
    frame = frame.id;
  }
  return `/api/frames/${frame}/image${thumb ? "?thumb=1" : ""}`;
};
export const exportUrl = (id: string, format: string) => `/api/meetings/${id}/export?format=${format}`;

// ---------- SSE ----------
type Listener = (e: MeetingEvent) => void;
const listeners = new Set<Listener>();
let source: EventSource | null = null;
let retryTimer: number | undefined;
let connState: "open" | "connecting" | "closed" = "closed";
const connListeners = new Set<(s: typeof connState) => void>();

function setConn(s: typeof connState) {
  connState = s;
  connListeners.forEach((l) => l(s));
}

function connect() {
  if (source || listeners.size === 0) return;
  setConn("connecting");
  const es = new EventSource("/api/events", { withCredentials: true });
  source = es;
  es.onopen = () => setConn("open");
  es.addEventListener("meeting", (msg) => {
    try {
      const data = JSON.parse((msg as MessageEvent).data) as MeetingEvent;
      listeners.forEach((l) => l(data));
    } catch {
      /* ignore malformed */
    }
  });
  es.onerror = () => {
    es.close();
    source = null;
    setConn("closed");
    window.clearTimeout(retryTimer);
    retryTimer = window.setTimeout(connect, 3000);
  };
}

export function subscribeMeetings(l: Listener): () => void {
  listeners.add(l);
  connect();
  return () => {
    listeners.delete(l);
    if (listeners.size === 0 && source) {
      source.close();
      source = null;
      setConn("closed");
    }
  };
}

export function subscribeConnection(l: (s: typeof connState) => void): () => void {
  connListeners.add(l);
  l(connState);
  return () => connListeners.delete(l);
}
