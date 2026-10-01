import { Feather, Library, LogOut, MessageSquareText, Monitor, Moon, Plus, Search, Settings, Sun, UploadCloud } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { subscribeConnection } from "../api/client";
import type { Meeting, User } from "../api/types";
import { useMeetings } from "../lib/meetings";
import { linkClick, navigate, type Route } from "../lib/router";
import { meetingPhase } from "../lib/status";
import { useTheme, type ThemePref } from "../lib/theme";
import { isTyping, openFilePicker, openPalette, openShortcuts, queueFiles, registerFilePicker } from "../lib/ui";
import { onUploadComplete, useUploads } from "../lib/uploads";
import { toast } from "../lib/toast";
import { displayName } from "../lib/people";
import { AvatarStack } from "./Avatars";
import { NamePrompt } from "./NamePrompt";
import { Palette } from "./Palette";
import { PersonAvatar } from "./PersonAvatar";
import { Shortcuts } from "./Shortcuts";
import { UploadSheet } from "./UploadSheet";

interface Props {
  user: User;
  route: Route;
  onLogout: () => void;
  onUser: (u: User) => void;
  children: ReactNode;
}

const ACCEPT = "video/*,audio/*,.mkv,.mov,.mp4,.webm,.m4a,.mp3,.wav,.flac,.ogg,.opus";
const NEXT_THEME: Record<ThemePref, ThemePref> = { system: "light", light: "dark", dark: "system" };
const THEME_ICON: Record<ThemePref, ReactNode> = { system: <Monitor size={14} />, light: <Sun size={14} />, dark: <Moon size={14} /> };
const THEME_LABEL: Record<ThemePref, string> = { system: "Match system", light: "Light", dark: "Dark" };
const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

function ProcessingRow({ m, current }: { m: Meeting; current: boolean }) {
  const p = meetingPhase(m);
  const pct = Math.round(p.overall * 100);
  return (
    <a href={`/m/${m.id}`} onClick={linkClick} aria-current={current ? "page" : undefined} className={`proc tone-${p.tone}`} title={p.reason ?? p.label}>
      <span className="proc-top">
        <i className="proc-dot" aria-hidden />
        <span className="t">{m.title}</span>
        <span className="proc-state">{p.tone === "run" ? `${pct}%` : p.label}</span>
      </span>
      <span className={`progress thin ${p.tone === "run" ? "run" : p.tone}`} aria-hidden>
        <i style={{ width: `${Math.max(4, pct)}%` }} />
      </span>
    </a>
  );
}

export function Shell({ user, route, onLogout, onUser, children }: Props) {
  const { meetings } = useMeetings();
  const uploads = useUploads();
  const [theme, setTheme] = useTheme();
  const [conn, setConn] = useState<"open" | "connecting" | "closed">("closed");
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const depth = useRef(0);
  useEffect(() => subscribeConnection(setConn), []);

  useEffect(() => {
    registerFilePicker(() => fileInput.current?.click());
    return () => registerFilePicker(null);
  }, []);

  useEffect(
    () =>
      onUploadComplete((id) =>
        toast("Upload finished. Processing has started.", "info", id ? { label: "Open", run: () => navigate(`/m/${id}`) } : undefined, 6000),
      ),
    [],
  );

  // Drop recordings anywhere.
  useEffect(() => {
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current++;
      setDragging(true);
    };
    const over = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    };
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setDragging(false);
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth.current = 0;
      setDragging(false);
      if (e.dataTransfer?.files.length) queueFiles(e.dataTransfer.files);
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, []);

  // Global keys: ⌘K switcher, ? shortcuts, U upload.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openPalette();
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e)) return;
      if (e.key === "?") {
        e.preventDefault();
        openShortcuts();
      } else if (e.key === "u" || e.key === "U") {
        e.preventDefault();
        openFilePicker();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Processing shows your own uploads; meetings shared with you still appear under Recent.
  const busy = meetings.filter((m) => (m.access ?? "owner") === "owner" && meetingPhase(m).tone !== "ok");
  const ready = meetings.filter((m) => meetingPhase(m).tone === "ok");
  const running = busy.filter((m) => meetingPhase(m).tone === "run").length;
  const active = uploads.filter((u) => u.status === "uploading" || u.status === "starting");
  const upPct = active.length ? Math.round((active.reduce((a, u) => a + u.sent, 0) / Math.max(1, active.reduce((a, u) => a + u.size, 0))) * 100) : 0;
  const recent = ready.slice(0, 6);

  const cur = (name: Route["name"], id?: string) =>
    route.name === name && (id === undefined || route.id === id) ? ("page" as const) : undefined;

  return (
    <div className="shell">
      <nav className="rail" aria-label="Main">
        <a className="rail-brand" href="/" onClick={linkClick} aria-label="Quill library">
          <span className="brand-mark" aria-hidden>
            <Feather size={14} />
          </span>
          <span className="label">Quill</span>
        </a>
        <button type="button" className="jump" onClick={openPalette} title={`Search or jump to (${isMac ? "⌘" : "Ctrl+"}K)`}>
          <Search size={14} />
          <span className="label jump-text">Search or jump to…</span>
          <kbd className="label">{isMac ? "⌘K" : "Ctrl K"}</kbd>
        </button>
        <div className="nav">
          <a href="/" onClick={linkClick} aria-current={cur("library")} title="Library">
            <Library size={16} />
            <span className="label">Library</span>
            {meetings.length > 0 && <span className="count">{meetings.length}</span>}
          </a>
          <a href="/search" onClick={linkClick} aria-current={cur("search")} title="Search transcripts">
            <MessageSquareText size={16} />
            <span className="label">Search transcripts</span>
          </a>
          <a href="/settings" onClick={linkClick} aria-current={cur("settings")} title="Settings">
            <Settings size={16} />
            <span className="label">Settings</span>
          </a>
        </div>
        <div className="rail-scroll">
          {busy.length > 0 && (
            <>
              <div className="nav-section">
                Processing <span className="n">{busy.length}</span>
              </div>
              <div className="procs">
                {busy.slice(0, 5).map((m) => (
                  <ProcessingRow key={m.id} m={m} current={route.name === "meeting" && route.id === m.id} />
                ))}
              </div>
            </>
          )}
          {recent.length > 0 && (
            <>
              <div className="nav-section">Recent</div>
              <div className="nav rail-recent">
                {recent.map((m) => (
                  <a key={m.id} href={`/m/${m.id}`} onClick={linkClick} aria-current={cur("meeting", m.id)} title={m.title}>
                    {m.speakers?.length ? <AvatarStack speakers={m.speakers} max={2} size={14} /> : <i className="dot-lg" aria-hidden />}
                    <span className="t">{m.title}</span>
                  </a>
                ))}
              </div>
            </>
          )}
        </div>
        <div className="rail-foot">
          {active.length > 0 && (
            <div className="status-line up" title={`Uploading ${active.length} file(s)`}>
              <UploadCloud size={12} />
              <span className="label">
                Uploading {active.length} · {upPct}%
              </span>
            </div>
          )}
          <div className="status-line" title={conn === "open" ? "Live updates connected" : "Reconnecting live updates"}>
            <i className={`dot ${conn === "open" ? "live" : "off"}`} />
            <span className="label">{conn === "open" ? (running ? `Live · ${running} processing` : "Live") : "Reconnecting…"}</span>
          </div>
          <div className="account">
            <a className="account-link" href="/settings" onClick={linkClick} title={`${user.email} · Account settings`}>
              <PersonAvatar user={user} size={24} />
              <span className="label who">
                <b>{displayName(user)}</b>
                <span>{user.is_admin ? "Admin" : "Member"}</span>
              </span>
            </a>
            <button
              type="button"
              className="icon-btn sm"
              onClick={() => setTheme(NEXT_THEME[theme])}
              aria-label={`Appearance: ${THEME_LABEL[theme]}. Switch to ${THEME_LABEL[NEXT_THEME[theme]]}`}
              title={`Appearance: ${THEME_LABEL[theme]}`}
            >
              {THEME_ICON[theme]}
            </button>
            <button type="button" className="icon-btn sm" onClick={onLogout} aria-label="Sign out" title="Sign out">
              <LogOut size={14} />
            </button>
          </div>
        </div>
      </nav>
      <main className="main sheet" id="main">
        <NamePrompt user={user} onUser={onUser} />
        {children}
      </main>
      <nav className="tabbar" aria-label="Main">
        <a href="/" onClick={linkClick} aria-current={cur("library")}>
          <Library size={20} />
          Library
        </a>
        <a href="/search" onClick={linkClick} aria-current={cur("search")}>
          <Search size={20} />
          Search
        </a>
        <button type="button" className="tab-up" onClick={openFilePicker} aria-label="Upload a recording">
          <span>
            <Plus size={22} />
          </span>
        </button>
        <a href="/settings" onClick={linkClick} aria-current={cur("settings")}>
          <Settings size={20} />
          Settings
        </a>
        <button type="button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
          {theme === "dark" ? <Sun size={20} /> : <Moon size={20} />}
          Theme
        </button>
      </nav>
      <input
        ref={fileInput}
        type="file"
        multiple
        accept={ACCEPT}
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(e) => {
          if (e.target.files?.length) queueFiles(e.target.files);
          e.target.value = "";
        }}
      />
      <div className={`drop-overlay ${dragging ? "on" : ""}`} aria-hidden={!dragging}>
        <div className="drop-card">
          <span className="drop-icon">
            <UploadCloud size={28} />
          </span>
          <strong>Drop to transcribe</strong>
          <span>Video or audio, up to 10 GB each</span>
        </div>
      </div>
      <Palette user={user} theme={theme} setTheme={setTheme} />
      <Shortcuts />
      <UploadSheet />
    </div>
  );
}
