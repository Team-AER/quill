import { AudioLines, CornerDownLeft, Keyboard, Library, MessageSquareText, Moon, Settings, Sun, Upload, UserPlus, UserRound, Users, Video } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Meeting, User } from "../api/types";
import { useMeetings } from "../lib/meetings";
import { navigate } from "../lib/router";
import { meetingPhase } from "../lib/status";
import type { ThemePref } from "../lib/theme";
import { formatDate, formatDuration } from "../lib/time";
import { highlightParts } from "../lib/transcript";
import { closePalette, openFilePicker, openShortcuts, useUI } from "../lib/ui";

interface Item {
  id: string;
  group: "Meetings" | "Search" | "Actions";
  icon: ReactNode;
  label: string;
  hl?: string;
  sub?: ReactNode;
  href?: string;
  run: () => void;
}

const matches = (title: string, q: string) => {
  const t = title.toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => t.includes(w));
};

export function Palette({ user, theme, setTheme }: { user: User; theme: ThemePref; setTheme: (t: ThemePref) => void }) {
  const { palette } = useUI();
  const { meetings } = useMeetings();
  const ref = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (palette && !d.open) {
      setQ("");
      setSel(0);
      d.showModal();
      input.current?.focus();
    }
    if (!palette && d.open) d.close();
  }, [palette]);

  const items = useMemo(() => {
    const query = q.trim();
    const go = (href: string) => () => {
      closePalette();
      navigate(href);
    };
    const meetingItem = (m: Meeting): Item => {
      const phase = meetingPhase(m);
      return {
        id: `m:${m.id}`,
        group: "Meetings",
        icon: m.mode === "audio" ? <AudioLines size={14} /> : <Video size={14} />,
        label: m.title,
        hl: query,
        sub: phase.tone === "ok" ? `${formatDate(m.created_at)} · ${formatDuration(m.duration_s)}` : phase.label,
        href: `/m/${m.id}`,
        run: go(`/m/${m.id}`),
      };
    };
    const found = (query ? meetings.filter((m) => matches(m.title, query)) : meetings).slice(0, query ? 8 : 5).map(meetingItem);
    const out: Item[] = [...found];
    if (query.length >= 2)
      out.push({
        id: "search",
        group: "Search",
        icon: <MessageSquareText size={14} />,
        label: `Search every transcript for “${query}”`,
        href: `/search?q=${encodeURIComponent(query)}`,
        run: go(`/search?q=${encodeURIComponent(query)}`),
      });
    const next: ThemePref = theme === "dark" ? "light" : "dark";
    const actions: Item[] = [
      { id: "upload", group: "Actions", icon: <Upload size={14} />, label: "Upload a recording", sub: <kbd>U</kbd>, run: () => (closePalette(), openFilePicker()) },
      { id: "library", group: "Actions", icon: <Library size={14} />, label: "Go to library", href: "/", run: go("/") },
      { id: "theme", group: "Actions", icon: next === "dark" ? <Moon size={14} /> : <Sun size={14} />, label: `Switch to ${next} appearance`, run: () => (setTheme(next), closePalette()) },
      { id: "keys", group: "Actions", icon: <Keyboard size={14} />, label: "Keyboard shortcuts", sub: <kbd>?</kbd>, run: openShortcuts },
      { id: "account", group: "Actions", icon: <UserRound size={14} />, label: "Account settings", sub: "Name, password, devices", href: "/settings", run: go("/settings") },
      ...(user.is_admin
        ? [
            { id: "invite", group: "Actions" as const, icon: <UserPlus size={14} />, label: "Invite someone", href: "/settings/people?invite=1", run: go("/settings/people?invite=1") },
            { id: "people", group: "Actions" as const, icon: <Users size={14} />, label: "People", sub: "Roles, password resets, invites", href: "/settings/people", run: go("/settings/people") },
            { id: "system", group: "Actions" as const, icon: <Settings size={14} />, label: "System settings", sub: "Models, retention", href: "/settings/system", run: go("/settings/system") },
          ]
        : []),
    ];
    out.push(...(query ? actions.filter((a) => matches(a.label, query)) : actions));
    return out;
  }, [q, meetings, theme, setTheme, user.is_admin]);

  useEffect(() => setSel(0), [q]);
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-i="${sel}"]`)?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown" || (e.key === "n" && e.ctrlKey)) {
      e.preventDefault();
      setSel((s) => Math.min(items.length - 1, s + 1));
    } else if (e.key === "ArrowUp" || (e.key === "p" && e.ctrlKey)) {
      e.preventDefault();
      setSel((s) => Math.max(0, s - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const it = items[sel];
      if (!it) return;
      if ((e.metaKey || e.ctrlKey) && it.href) {
        window.open(it.href, "_blank", "noopener");
        closePalette();
      } else it.run();
    }
  };

  let last = "";
  return (
    <dialog
      ref={ref}
      className="palette"
      aria-label="Search or jump to"
      onCancel={(e) => (e.preventDefault(), closePalette())}
      onClick={(e) => e.target === e.currentTarget && closePalette()}
    >
      <div className="pal" onKeyDown={onKey}>
        <label className="pal-input">
          <span className="sr-only">Search meetings and actions</span>
          <input
            ref={input}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search meetings, or type a command"
            role="combobox"
            aria-expanded="true"
            aria-controls="pal-list"
            aria-activedescendant={items[sel] ? `pal-${sel}` : undefined}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd>esc</kbd>
        </label>
        <div className="pal-list" id="pal-list" role="listbox" ref={list}>
          {items.length === 0 && <div className="pal-empty">Nothing matches “{q.trim()}”.</div>}
          {items.map((it, i) => {
            const head = it.group !== last ? it.group : null;
            last = it.group;
            return (
              <div key={it.id} role="presentation">
                {head && <div className="pal-head">{head === "Meetings" && !q.trim() ? "Recent meetings" : head}</div>}
                <div
                  id={`pal-${i}`}
                  data-i={i}
                  role="option"
                  aria-selected={i === sel}
                  className="pal-item"
                  onMouseMove={() => i !== sel && setSel(i)}
                  onClick={(e) => {
                    if ((e.metaKey || e.ctrlKey) && it.href) {
                      window.open(it.href, "_blank", "noopener");
                      closePalette();
                    } else it.run();
                  }}
                >
                  <span className="pal-icon">{it.icon}</span>
                  <span className="pal-label">
                    {it.hl ? highlightParts(it.label, it.hl).map((p, k) => (p.hit ? <mark key={k}>{p.text}</mark> : <span key={k}>{p.text}</span>)) : it.label}
                  </span>
                  {it.sub && <span className="pal-sub">{it.sub}</span>}
                  {i === sel && <CornerDownLeft size={13} className="pal-enter" aria-hidden />}
                </div>
              </div>
            );
          })}
        </div>
        <div className="pal-foot" aria-hidden>
          <span>
            <kbd>↑</kbd> <kbd>↓</kbd> move
          </span>
          <span>
            <kbd>↵</kbd> open
          </span>
          <span>
            <kbd>⌘</kbd> <kbd>↵</kbd> new tab
          </span>
        </div>
      </div>
    </dialog>
  );
}
