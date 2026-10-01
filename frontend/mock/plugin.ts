// Dev mock of the Quill HTTP API (docs/CONTRACT.md) as a Vite middleware.
// Active when QUILL_API is not set. In-memory state, resets on dev-server restart.
//
// Test login (mock only): admin@quill.test / quill-dev
// QUILL_MOCK_SETUP=1 starts with no users so the first-run setup screen shows.

import type { Plugin, Connect } from "vite";
import type { IncomingMessage, ServerResponse } from "node:http";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, statSync, createReadStream, renameSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { randomBytes } from "node:crypto";
import { buildFixtures, frameSvg, newUploadedMeeting, STAGES, type MMeeting } from "./fixtures.ts";
import { MOCK_EMAIL, MOCK_PASSWORD, mockAccounts } from "./accounts.ts";

interface Upload {
  id: string;
  length: number;
  offset: number;
  metadata: Record<string, string>;
  meetingId?: string;
}

const STAGE_SECONDS: Record<string, number> = {
  probe: 3,
  extract_audio: 5,
  diarize: 12,
  transcribe: 20,
  key_moments: 6,
  frames: 14,
  synthesize: 10,
};

export function quillMock(): Plugin {
  const uploads = new Map<string, Upload>();
  let meetings: MMeeting[] = buildFixtures();
  const settings: Record<string, unknown> = { stt_model: "aer-stt-v1", video_retention_days: 14 };
  const catalog: Record<string, string> = { "aer-stt-v1": "disabled", "aer-stt-qwen3": "ready" };
  const sse = new Map<ServerResponse, number>(); // stream -> user id
  const wavCache = new Map<number, Buffer>();

  const here = path.dirname(fileURLToPath(import.meta.url));
  const cacheDir = path.join(here, ".cache");
  const videoPath = path.join(cacheDir, "demo-3600.mp4");
  let videoReady = existsSync(videoPath);

  function ensureVideo(log: (m: string) => void) {
    if (videoReady) return;
    mkdirSync(cacheDir, { recursive: true });
    const tmp = videoPath + ".part.mp4";
    const args = [
      "-y", "-loglevel", "error",
      "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=2:duration=3600",
      "-f", "lavfi", "-i", "sine=frequency=180:sample_rate=16000:duration=3600",
      "-c:v", "libx264", "-preset", "ultrafast", "-crf", "34", "-g", "20", "-pix_fmt", "yuv420p",
      "-c:a", "aac", "-b:a", "24k", "-movflags", "+faststart", tmp,
    ];
    try {
      const p = spawn("ffmpeg", args, { stdio: "ignore" });
      p.on("error", () => log("[quill-mock] ffmpeg not found; video meetings will play generated audio"));
      p.on("exit", (code) => {
        if (code === 0) {
          renameSync(tmp, videoPath);
          videoReady = true;
          log("[quill-mock] demo video ready");
        }
      });
    } catch {
      /* no ffmpeg */
    }
  }

  // ---------- helpers ----------
  const now = () => new Date().toISOString();
  const find = (id: string) => meetings.find((m) => m.id === id);

  function send(res: ServerResponse, status: number, body?: unknown, headers: Record<string, string> = {}) {
    if (body === undefined) {
      res.writeHead(status, headers);
      res.end();
      return;
    }
    res.writeHead(status, { "Content-Type": "application/json", ...headers });
    res.end(JSON.stringify(body));
  }
  const err = (res: ServerResponse, status: number, msg: string) => send(res, status, { error: msg });

  async function readBody(req: IncomingMessage): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    return Buffer.concat(chunks);
  }
  async function readJson(req: IncomingMessage): Promise<Record<string, unknown>> {
    const b = await readBody(req);
    try {
      return b.length ? JSON.parse(b.toString("utf8")) : {};
    } catch {
      return {};
    }
  }
  function cookie(req: IncomingMessage, name: string): string | null {
    const m = (req.headers.cookie ?? "").split(/;\s*/).find((c) => c.startsWith(name + "="));
    return m ? decodeURIComponent(m.slice(name.length + 1)) : null;
  }
  const accounts = mockAccounts({ send, err, readJson, cookie });

  // ---------- sharing (mirrors quill/sharing.py) ----------
  type Level = "view" | "edit";
  type Access = "owner" | Level;
  const RANK: Record<Access, number> = { view: 1, edit: 2, owner: 3 };
  const everyone = new Map<string, Level>();
  const shares = new Map<string, Map<number, Level>>();
  if (!process.env.QUILL_MOCK_SETUP) {
    // Priya's design review is shared with the admin (edit); Tomás shared his retro with everyone;
    // the admin shared the roadmap sync with Priya.
    const own = (id: string, uid: number) => {
      const m = find(id);
      if (m) m.owner_id = uid;
    };
    own("m_design", 2);
    shares.set("m_design", new Map([[1, "edit"]]));
    own("m_retro", 3);
    everyone.set("m_retro", "view");
    shares.set("m_roadmap", new Map([[2, "view"]]));
  }
  function access(m: MMeeting, uid: number): Access | null {
    if (m.owner_id === uid) return "owner";
    const levels = [shares.get(m.id)?.get(uid), everyone.get(m.id)].filter(Boolean) as Level[];
    return levels.length ? (levels.includes("edit") ? "edit" : "view") : null;
  }
  function sharingFields(m: MMeeting, uid: number) {
    const a = access(m, uid) ?? "view";
    return {
      access: a,
      owner: accounts.person(m.owner_id),
      everyone_access: everyone.get(m.id) ?? null,
      ...(a === "owner" ? { shared_with: shares.get(m.id)?.size ?? 0 } : {}),
    };
  }
  function sharingState(m: MMeeting) {
    const people = [...(shares.get(m.id) ?? new Map<number, Level>()).entries()]
      .map(([uid, level]) => ({ ...accounts.person(uid)!, user_id: uid, access: level, shared_at: null, disabled: !accounts.isActive(uid) }))
      .filter((p) => p.email)
      .sort((a, b) => (a.name || a.email).localeCompare(b.name || b.email));
    return { owner: accounts.person(m.owner_id), everyone: everyone.get(m.id) ?? null, people };
  }
  function overall(m: MMeeting): [string | null, number] {
    const rel = m.stages.filter((s) => s.status !== "skipped");
    if (!rel.length) return [null, 1];
    const cur = rel.find((s) => s.status !== "done")?.name ?? null;
    const tot = rel.reduce((a, s) => a + (s.status === "done" ? 1 : (s.progress ?? 0)), 0);
    return [cur, Math.round((tot / rel.length) * 1e4) / 1e4];
  }
  // Mirrors backend/quill/api.py meeting_json (list) and meeting_detail.
  function listMeeting(m: MMeeting, uid: number) {
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { lines, frames, moments, notes, _final, stages, speakers, owner_id, ...rest } = m;
    const [current_stage, progress] = overall(m);
    // card_extras(): cover frame, speakers, one-line gist, action count
    const cover = [...m.frames].sort((a, b) => Number((b.relevance ?? 1) > 0) - Number((a.relevance ?? 1) > 0) || a.t - b.t)[0];
    const n = m.notes as { tldr?: string[]; summary?: string; action_items?: unknown[] } | null;
    const gist = n ? (n.tldr?.[0] ?? n.summary?.split(/(?<=[.!?])\s/)[0] ?? null) : null;
    return {
      ...rest,
      has_video: Boolean(m.has_video),
      current_stage,
      progress,
      speaker_count: m.speakers.length,
      cover_url: cover ? `/api/frames/${cover.id}/image?thumb=1` : null,
      speakers: speakersJson(m).map(({ label, display_name, name, color, talk_time_s }) => ({ label, display_name, name, color, talk_time_s })),
      gist: gist ? gist.replace(/[*_`#]+/g, "").slice(0, 280) : null,
      action_count: n ? (n.action_items?.length ?? 0) : null,
      ...sharingFields(m, uid),
    };
  }
  function speakersJson(m: MMeeting) {
    const talk = new Map<string, number>();
    for (const l of m.lines) talk.set(l.speaker, (talk.get(l.speaker) ?? 0) + (l.end - l.start));
    return m.speakers.map((s) => ({
      ...s,
      name: s.display_name || `Speaker ${s.label.slice(1)}`,
      talk_time_s: Math.round((talk.get(s.label) ?? 0) * 100) / 100,
    }));
  }
  function publicMeeting(m: MMeeting, uid: number) {
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { speaker_count, ...base } = listMeeting(m, uid);
    return { ...base, stages: m.stages, speakers: speakersJson(m), has_notes: m.notes != null };
  }
  function broadcast(m: MMeeting) {
    const [current_stage, progress] = overall(m);
    const data = JSON.stringify({ id: m.id, status: m.status, error: m.error, title: m.title, mode: m.mode, duration_s: m.duration_s, current_stage, progress, stages: m.stages });
    for (const [r, uid] of sse) if (access(m, uid)) r.write(`event: meeting\ndata: ${data}\n\n`);
  }

  function sttReady() {
    const s = catalog[String(settings.stt_model)];
    return s === "ready" || s === "degraded";
  }

  // ---------- simulator ----------
  function applyFinal(m: MMeeting, stage: string) {
    const f = m._final;
    if (!f) return;
    if (stage === "diarize" && f.speakers) m.speakers = f.speakers;
    if (stage === "transcribe" && f.lines) m.lines = f.lines;
    if (stage === "key_moments" && f.moments) m.moments = f.moments;
    if (stage === "frames" && f.frames) m.frames = f.frames;
    if (stage === "synthesize" && f.notes) {
      m.notes = f.notes;
      const sugg = (f.notes as { speaker_suggestions?: { label: string; name: string; evidence_t: number }[] }).speaker_suggestions ?? [];
      for (const s of sugg) {
        const sp = m.speakers.find((x) => x.label === s.label);
        if (sp && !sp.display_name) {
          sp.suggested_name = s.name;
          sp.suggestion_evidence_t = s.evidence_t;
        }
      }
    }
  }

  function tick() {
    for (const m of meetings) {
      if (m.status === "done" || m.status === "failed") continue;
      let st = m.stages.find((s) => s.status === "running" || s.status === "paused");
      if (!st) {
        st = m.stages.find((s) => s.status === "pending");
        if (!st) {
          m.status = "done";
          broadcast(m);
          continue;
        }
        st.status = "running";
        st.progress = 0;
        st.started_at = now();
        m.status = "running";
      }
      if (st.name === "transcribe") {
        if (!sttReady()) {
          if (st.status !== "paused") {
            st.status = "paused";
            st.detail = "STT paused: model disabled in llm-proxy";
            m.status = "paused";
            broadcast(m);
          }
          continue;
        }
        if (st.status === "paused") {
          st.status = "running";
          st.detail = null;
          m.status = "running";
        }
        // reveal transcript progressively
        const all = m._final?.lines;
        if (all) {
          const n = Math.floor(all.length * Math.min(1, (st.progress ?? 0) + 0.05));
          if (n > m.lines.length) m.lines = all.slice(0, n);
        }
      }
      const step = 0.5 / (STAGE_SECONDS[st.name] ?? 8);
      st.progress = Math.min(1, (st.progress ?? 0) + step);
      if (st.name === "transcribe") {
        const total = 48;
        st.detail = `Segment ${Math.ceil((st.progress ?? 0) * total)} of ${total}`;
      } else if (st.name === "frames") {
        const total = m._final?.frames?.length || 12;
        st.detail = `Analysing frame ${Math.max(1, Math.ceil((st.progress ?? 0) * total))} of ${total}`;
      } else if (st.name === "diarize") {
        st.detail = `${Math.round((st.progress ?? 0) * (m.duration_s / 60))} of ${Math.round(m.duration_s / 60)} min`;
      } else st.detail = null;
      if (st.progress >= 1) {
        st.status = "done";
        st.finished_at = now();
        st.detail = null;
        applyFinal(m, st.name);
        if (!m.stages.some((s) => s.status === "pending")) m.status = "done";
      }
      broadcast(m);
    }
  }

  function rerun(m: MMeeting, stage: string, options: Record<string, unknown>) {
    const idx = STAGES.indexOf(stage);
    if (idx < 0) return false;
    m._final = m._final ?? {};
    const f = m._final;
    const later = STAGES.slice(idx);
    if (later.includes("diarize") && m.speakers.length) f.speakers = f.speakers ?? m.speakers;
    if (later.includes("transcribe")) {
      if (m.lines.length && !(f.lines && f.lines.length >= m.lines.length)) f.lines = m.lines;
      m.lines = [];
    }
    if (later.includes("key_moments")) {
      if (m.moments.length) f.moments = m.moments;
      m.moments = [];
    }
    if (later.includes("frames")) {
      if (m.frames.length) f.frames = m.frames;
      m.frames = [];
    }
    if (later.includes("synthesize")) {
      if (m.notes) f.notes = m.notes;
      m.notes = null;
    }
    if (options.expected_speakers) m.expected_speakers = Number(options.expected_speakers);
    m.stages = m.stages.map((s, i) =>
      i >= idx && s.status !== "skipped"
        ? { ...s, status: "pending", progress: 0, detail: null, started_at: null, finished_at: null, error: null }
        : s,
    );
    m.status = "queued";
    m.error = null;
    broadcast(m);
    return true;
  }

  // ---------- media ----------
  function wav(seconds: number, m: MMeeting): Buffer {
    const cached = wavCache.get(seconds);
    if (cached) return cached;
    const rate = 8000;
    const n = Math.floor(seconds * rate);
    const buf = Buffer.alloc(44 + n);
    buf.write("RIFF", 0);
    buf.writeUInt32LE(36 + n, 4);
    buf.write("WAVEfmt ", 8);
    buf.writeUInt32LE(16, 16);
    buf.writeUInt16LE(1, 20);
    buf.writeUInt16LE(1, 22);
    buf.writeUInt32LE(rate, 24);
    buf.writeUInt32LE(rate, 28);
    buf.writeUInt16LE(1, 32);
    buf.writeUInt16LE(8, 34);
    buf.write("data", 36);
    buf.writeUInt32LE(n, 40);
    buf.fill(128, 44);
    // soft tone per speaker during each line, so seeking is audible
    const lines = m.lines.length ? m.lines : (m._final?.lines ?? []);
    for (const l of lines) {
      const f = 140 + Number(l.speaker.slice(1)) * 45;
      const a = Math.floor(l.start * rate);
      const b = Math.min(n, Math.floor(l.end * rate));
      for (let i = a; i < b; i++) {
        const env = Math.min(1, (i - a) / 400, (b - i) / 400) * (0.6 + 0.4 * Math.sin(i / 900));
        buf[44 + i] = 128 + Math.round(18 * env * Math.sin((2 * Math.PI * f * i) / rate));
      }
    }
    wavCache.set(seconds, buf);
    return buf;
  }

  function serveRange(req: IncomingMessage, res: ServerResponse, size: number, type: string, open: (s: number, e: number) => NodeJS.ReadableStream | Buffer) {
    const range = req.headers.range;
    let start = 0;
    let end = size - 1;
    let status = 200;
    if (range) {
      const mm = /bytes=(\d*)-(\d*)/.exec(range);
      if (mm) {
        if (mm[1]) start = Number(mm[1]);
        if (mm[2]) end = Math.min(size - 1, Number(mm[2]));
        if (!mm[1] && mm[2]) {
          start = size - Number(mm[2]);
          end = size - 1;
        }
        status = 206;
      }
    }
    if (start > end || start >= size) {
      res.writeHead(416, { "Content-Range": `bytes */${size}` });
      res.end();
      return;
    }
    const headers: Record<string, string> = {
      "Content-Type": type,
      "Accept-Ranges": "bytes",
      "Content-Length": String(end - start + 1),
    };
    if (status === 206) headers["Content-Range"] = `bytes ${start}-${end}/${size}`;
    res.writeHead(status, headers);
    if (req.method === "HEAD") return res.end();
    const body = open(start, end);
    if (Buffer.isBuffer(body)) res.end(body);
    else body.pipe(res);
  }

  // ---------- exports ----------
  function pad(n: number, w = 2) {
    return String(n).padStart(w, "0");
  }
  function ts(t: number, sep: string) {
    const h = Math.floor(t / 3600);
    const mi = Math.floor((t % 3600) / 60);
    const s = Math.floor(t % 60);
    const ms = Math.round((t % 1) * 1000);
    return `${pad(h)}:${pad(mi)}:${pad(s)}${sep}${pad(ms, 3)}`;
  }
  function spName(m: MMeeting, label: string) {
    const s = m.speakers.find((x) => x.label === label);
    return s?.display_name || `Speaker ${label.slice(1)}`;
  }
  function exportBody(m: MMeeting, fmt: string): [string, string] | null {
    const L = m.lines;
    switch (fmt) {
      case "txt":
        return ["text/plain", L.map((l) => `[${ts(l.start, ".").slice(0, 8)}] ${spName(m, l.speaker)}: ${l.text}`).join("\n")];
      case "srt":
        return ["application/x-subrip", L.map((l, i) => `${i + 1}\n${ts(l.start, ",")} --> ${ts(l.end, ",")}\n${spName(m, l.speaker)}: ${l.text}\n`).join("\n")];
      case "vtt":
        return ["text/vtt", "WEBVTT\n\n" + L.map((l) => `${ts(l.start, ".")} --> ${ts(l.end, ".")}\n<v ${spName(m, l.speaker)}>${l.text}\n`).join("\n")];
      case "rttm":
        return ["text/plain", L.map((l) => `SPEAKER ${m.id} 1 ${l.start.toFixed(3)} ${(l.end - l.start).toFixed(3)} <NA> <NA> ${l.speaker} <NA> <NA>`).join("\n")];
      case "json":
        return ["application/json", JSON.stringify({ meeting: publicMeeting(m, m.owner_id), transcript: L, notes: m.notes, frames: m.frames }, null, 2)];
      case "md": {
        const n = m.notes as { tldr: string[]; summary: string; action_items: { owner: string; task: string }[] } | null;
        if (!n) return ["text/markdown", `# ${m.title}\n\n_No notes yet._\n`];
        return [
          "text/markdown",
          `# ${m.title}\n\n## TL;DR\n${n.tldr.map((x) => `- ${x}`).join("\n")}\n\n## Summary\n${n.summary}\n\n## Action items\n${n.action_items.map((a) => `- [ ] ${a.owner ? spName(m, a.owner) + ": " : ""}${a.task}`).join("\n")}\n`,
        ];
      }
    }
    return null;
  }

  // ---------- tus ----------
  const TUS = { "Tus-Resumable": "1.0.0" };
  function parseMeta(h: string | undefined): Record<string, string> {
    const out: Record<string, string> = {};
    for (const pair of (h ?? "").split(",")) {
      const [k, v] = pair.trim().split(" ");
      if (k) out[k] = v ? Buffer.from(v, "base64").toString("utf8") : "";
    }
    return out;
  }

  async function handleTus(req: IncomingMessage, res: ServerResponse, id: string | null) {
    const method = req.headers["x-http-method-override"]?.toString() ?? req.method;
    if (method === "OPTIONS") {
      return send(res, 204, undefined, {
        ...TUS,
        "Tus-Version": "1.0.0",
        "Tus-Extension": "creation,termination,checksum",
        "Tus-Max-Size": String(10 * 1024 ** 3),
        "Tus-Checksum-Algorithm": "sha256",
      });
    }
    if (!id && method === "POST") {
      const length = Number(req.headers["upload-length"]);
      if (!Number.isFinite(length)) return err(res, 400, "Upload-Length required");
      if (length > 10 * 1024 ** 3) return err(res, 413, "File exceeds the 10 GiB limit");
      const up: Upload = { id: randomBytes(8).toString("hex"), length, offset: 0, metadata: parseMeta(req.headers["upload-metadata"] as string) };
      uploads.set(up.id, up);
      return send(res, 201, undefined, { ...TUS, Location: `/api/uploads/${up.id}`, "Access-Control-Expose-Headers": "Location, Quill-Meeting-Id" });
    }
    const up = id ? uploads.get(id) : undefined;
    if (!up) return send(res, 404, undefined, TUS);
    if (method === "HEAD") {
      return send(res, 200, undefined, { ...TUS, "Upload-Offset": String(up.offset), "Upload-Length": String(up.length), "Cache-Control": "no-store" });
    }
    if (method === "DELETE") {
      uploads.delete(up.id);
      return send(res, 204, undefined, TUS);
    }
    if (method === "PATCH") {
      const off = Number(req.headers["upload-offset"]);
      if (off !== up.offset) return send(res, 409, undefined, TUS);
      let n = 0;
      try {
        for await (const c of req) n += (c as Buffer).length;
      } catch {
        // Client paused/aborted mid-chunk: keep what arrived, like a real tus server.
        up.offset += n;
        return;
      }
      up.offset += n;
      const headers: Record<string, string> = { ...TUS, "Upload-Offset": String(up.offset) };
      if (up.offset >= up.length) {
        if (!up.meetingId) {
          const mid = "m_" + randomBytes(4).toString("hex");
          const m = newUploadedMeeting(mid, up.metadata, up.length);
          m.owner_id = accounts.currentUser(req)?.id ?? 1;
          meetings.unshift(m);
          up.meetingId = mid;
          broadcast(m);
        }
        headers["Quill-Meeting-Id"] = up.meetingId;
      }
      return send(res, 204, undefined, headers);
    }
    return err(res, 405, "method not allowed");
  }

  // ---------- router ----------
  const handler: Connect.NextHandleFunction = async (req, res, next) => {
    const url = new URL(req.url ?? "/", "http://mock");
    const p = url.pathname;
    if (!p.startsWith("/api/")) return next();
    const method = req.method ?? "GET";
    try {
      // --- auth (public), then everything else behind a session ---
      if (await accounts.handlePublic(req, res, p, method, url)) return;
      // frame images are fetched by <img>, still behind auth
      const user = accounts.currentUser(req);
      if (!user) return err(res, 401, "Sign in to continue.");
      if (await accounts.handleAuthed(req, res, p, method, url, user)) return;

      if (p === "/api/uploads" || p.startsWith("/api/uploads/")) {
        return await handleTus(req, res, p.split("/")[3] ?? null);
      }

      if (p === "/api/events") {
        res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
        res.write("retry: 3000\n\n");
        sse.set(res, user.id);
        // Like the API: every stream starts with a snapshot of the meetings you can open.
        for (const m of meetings) {
          if (!access(m, user.id)) continue;
          const [current_stage, progress] = overall(m);
          res.write(`event: meeting\ndata: ${JSON.stringify({ id: m.id, status: m.status, error: m.error, title: m.title, mode: m.mode, duration_s: m.duration_s, current_stage, progress, stages: m.stages })}\n\n`);
        }
        const hb = setInterval(() => res.write(": ping\n\n"), 15000);
        req.on("close", () => {
          clearInterval(hb);
          sse.delete(res);
        });
        return;
      }

      if (p === "/api/system") {
        const mk = (model: string) => ({ model, status: catalog[model] ?? "ready" });
        const busy = meetings.find((m) => m.status === "running");
        return send(res, 200, {
          stt: mk(String(settings.stt_model)),
          vision: { model: "google/gemma-4-12B-it-qat-w4a16-ct", status: "ready" },
          text: { model: "google/gemma-4-12B-it-qat-w4a16-ct", status: "degraded" },
          disk: { free_bytes: 118 * 1024 ** 3, total_bytes: 300 * 1024 ** 3, reserve_bytes: 5 * 1024 ** 3 },
          max_upload_bytes: 10 * 1024 ** 3,
          worker: {
            heartbeat_at: now(),
            queue_length: meetings.filter((m) => ["queued", "running", "paused"].includes(m.status)).length,
            meeting_id: busy?.id ?? null,
            stage: busy ? overall(busy)[0] : null,
          },
        });
      }
      if (p === "/api/settings") {
        if (method === "PATCH") {
          if (!user.is_admin) return err(res, 403, "Admins only");
          const b = await readJson(req);
          if (b.stt_model !== undefined) {
            const s = catalog[String(b.stt_model)];
            if (!s) return err(res, 400, "Unknown STT model.");
            if (s !== "ready" && s !== "degraded")
              return err(res, 409, `${b.stt_model} is ${s} in llm-proxy; only ready or degraded models can be selected.`);
            settings.stt_model = b.stt_model;
          }
          if (b.video_retention_days !== undefined) {
            const v = b.video_retention_days;
            if (typeof v !== "number" || !Number.isInteger(v) || v < -1 || v > 3650)
              return err(res, 400, "video_retention_days must be -1 (forever) or 0-3650.");
            settings.video_retention_days = v;
          }
        }
        return send(res, 200, {
          stt_model: settings.stt_model,
          text_model: "google/gemma-4-12B-it-qat-w4a16-ct",
          vision_model: "google/gemma-4-12B-it-qat-w4a16-ct",
          video_retention_days: settings.video_retention_days,
          stt_model_options: Object.entries(catalog).map(([model, status]) => ({ model, status, selectable: status === "ready" || status === "degraded" })),
        });
      }

      if (p === "/api/search") {
        const q = (url.searchParams.get("q") ?? "").trim().toLowerCase();
        if (q.length < 2) return send(res, 200, { transcript: [], frames: [] });
        const only = url.searchParams.get("meeting_id");
        const mark = (text: string) => {
          const i = text.toLowerCase().indexOf(q);
          if (i < 0) return text.slice(0, 120);
          const a = Math.max(0, i - 60);
          const b = Math.min(text.length, i + q.length + 60);
          return (a > 0 ? "…" : "") + text.slice(a, i) + "[[" + text.slice(i, i + q.length) + "]]" + text.slice(i + q.length, b) + (b < text.length ? "…" : "");
        };
        const transcript: unknown[] = [];
        const frames: unknown[] = [];
        for (const m of meetings) {
          if ((only && m.id !== only) || !access(m, user.id)) continue;
          for (const l of m.lines)
            if (l.text.toLowerCase().includes(q) && transcript.length < 50)
              transcript.push({ meeting_id: m.id, meeting_title: m.title, line_id: l.id, speaker: l.speaker, speaker_name: spName(m, l.speaker), start: l.start, end: l.end, text: l.text, snippet: mark(l.text) });
          for (const f of m.frames) {
            const hay = `${f.visible_text} ${f.caption}`;
            if (hay.toLowerCase().includes(q))
              frames.push({ meeting_id: m.id, meeting_title: m.title, frame_id: f.id, t: f.t, title: f.title, caption: f.caption, snippet: mark(hay.replace(/\n/g, " · ")), thumb_url: `/api/frames/${f.id}/image?thumb=1` });
          }
        }
        return send(res, 200, { transcript, frames });
      }

      let mm = /^\/api\/frames\/(\d+)\/image$/.exec(p);
      if (mm) {
        const id = Number(mm[1]);
        for (const m of meetings) {
          const i = m.frames.findIndex((f) => f.id === id);
          if (i >= 0 && access(m, user.id)) {
            res.writeHead(200, { "Content-Type": "image/svg+xml", "Cache-Control": "private, max-age=3600" });
            return res.end(frameSvg(m.frames[i], i));
          }
        }
        return err(res, 404, "No such frame");
      }

      if (p === "/api/directory") return send(res, 200, accounts.directory());
      if (p === "/api/meetings" && method === "GET")
        return send(res, 200, meetings.filter((m) => access(m, user.id)).map((m) => listMeeting(m, user.id)));

      mm = /^\/api\/meetings\/([^/]+)(?:\/(.+))?$/.exec(p);
      if (mm) {
        const m = find(mm[1]);
        const acc = m && access(m, user.id);
        if (!m || !acc) return err(res, 404, "Meeting not found");
        const sub = mm[2] ?? "";
        const needs = (level: Access) => RANK[acc] >= RANK[level];
        const writes = method !== "GET" && method !== "HEAD";
        if (writes && (sub === "" || /^speakers\//.test(sub)) && method !== "DELETE" && !needs("edit"))
          return err(res, 403, "You can view this meeting but not change it. Ask its owner for edit access.");
        if ((method === "DELETE" && sub === "") || sub === "rerun" || sub === "sharing")
          if (!needs("owner")) return err(res, 403, "Only the person who uploaded this meeting can do that.");
        if (sub === "sharing") {
          if (method === "PATCH") {
            const b = await readJson(req);
            if ("everyone" in b) {
              if (b.everyone) everyone.set(m.id, b.everyone as Level);
              else everyone.delete(m.id);
            }
            const map = shares.get(m.id) ?? new Map<number, Level>();
            for (const [k, v] of Object.entries((b.people as Record<string, Level | null>) ?? {})) {
              const uid = Number(k);
              if (uid === m.owner_id) return err(res, 400, "The owner always has access.");
              if (v && !accounts.isActive(uid)) return err(res, 400, "You can only share with active accounts on this Quill.");
              if (v) map.set(uid, v);
              else map.delete(uid);
            }
            shares.set(m.id, map);
            broadcast(m);
          }
          return send(res, 200, sharingState(m));
        }
        if (!sub) {
          if (method === "GET") return send(res, 200, publicMeeting(m, user.id));
          if (method === "PATCH") {
            const b = await readJson(req);
            if (typeof b.title === "string" && b.title.trim()) m.title = b.title.trim();
            broadcast(m);
            return send(res, 200, publicMeeting(m, user.id));
          }
          if (method === "DELETE") {
            meetings = meetings.filter((x) => x !== m);
            for (const [r] of sse) r.write(`event: meeting\ndata: ${JSON.stringify({ id: m.id, status: "deleted", stages: [] })}\n\n`);
            return send(res, 200, { ok: true });
          }
        }
        if (sub === "transcript") return send(res, 200, m.lines);
        if (sub === "notes")
          return send(res, 200, m.notes ? { version: 1, model: "google/gemma-4-12B-it-qat-w4a16-ct", created_at: m.created_at, notes: m.notes } : { version: null, model: null, created_at: null, notes: null });
        if (sub === "frames")
          return send(res, 200, m.frames.map(({ hue, ...f }) => (void hue, { ...f, image_url: `/api/frames/${f.id}/image`, thumb_url: `/api/frames/${f.id}/image?thumb=1` })));
        if (sub === "moments") return send(res, 200, m.moments);
        if (sub === "rerun" && method === "POST") {
          const b = await readJson(req);
          if (!rerun(m, String(b.stage), (b.options as Record<string, unknown>) ?? {})) return err(res, 400, "Unknown stage");
          return send(res, 200, publicMeeting(m, user.id));
        }
        if (sub === "speakers/merge" && method === "POST") {
          const b = await readJson(req);
          const from = String(b.from);
          const into = String(b.into);
          if (from === into || !m.speakers.find((s) => s.label === from) || !m.speakers.find((s) => s.label === into))
            return err(res, 400, "Pick two different speakers");
          m.lines = m.lines.map((l) => (l.speaker === from ? { ...l, speaker: into } : l));
          m.speakers = m.speakers.filter((s) => s.label !== from);
          return send(res, 200, speakersJson(m));
        }
        const sm = /^speakers\/(S\d+)$/.exec(sub);
        if (sm && method === "PATCH") {
          const b = await readJson(req);
          const sp = m.speakers.find((s) => s.label === sm[1]);
          if (!sp) return err(res, 404, "No such speaker");
          sp.display_name = b.display_name ? String(b.display_name).trim() : null;
          if (sp.display_name && sp.display_name === sp.suggested_name) sp.suggested_name = null;
          return send(res, 200, speakersJson(m));
        }
        if (sub === "media") {
          const useVideo = m.mode === "video" && !m.source_deleted_at && videoReady;
          if (useVideo) {
            const size = statSync(videoPath).size;
            return serveRange(req, res, size, "video/mp4", (s, e) => createReadStream(videoPath, { start: s, end: e }));
          }
          const buf = wav(m.duration_s, m);
          return serveRange(req, res, buf.length, "audio/wav", (s, e) => buf.subarray(s, e + 1));
        }
        if (sub === "export") {
          const fmt = url.searchParams.get("format") ?? "md";
          const out = exportBody(m, fmt);
          if (!out) return err(res, 400, "Unknown format");
          const ext = fmt === "md" ? "md" : fmt;
          res.writeHead(200, {
            "Content-Type": `${out[0]}; charset=utf-8`,
            "Content-Disposition": `attachment; filename="${m.title.replace(/[^\w.-]+/g, "_")}.${ext}"`,
          });
          return res.end(out[1]);
        }
      }
      return err(res, 404, `No mock for ${method} ${p}`);
    } catch (e) {
      if (!res.headersSent && !res.writableEnded) return err(res, 500, String(e));
    }
  };

  return {
    name: "quill-mock",
    configureServer(server) {
      const log = (m: string) => server.config.logger.info(m);
      ensureVideo(log);
      const timer = setInterval(tick, 500);
      server.httpServer?.on("close", () => clearInterval(timer));
      server.middlewares.use(handler);
      log(`[quill-mock] API mock active. Login: ${MOCK_EMAIL} / ${MOCK_PASSWORD}`);
    },
    configurePreviewServer(server) {
      const timer = setInterval(tick, 500);
      server.httpServer?.on("close", () => clearInterval(timer));
      server.middlewares.use(handler);
    },
  };
}

