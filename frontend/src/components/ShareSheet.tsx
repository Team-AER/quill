import { Building2, Check, Link2, Lock, Search, Users, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { ShareLevel, Sharing, Teammate } from "../api/types";
import { copyText, momentUrl } from "../lib/copy";
import { displayName } from "../lib/people";
import { linkClick } from "../lib/router";
import { errorText, toast } from "../lib/toast";
import { PersonAvatar } from "./PersonAvatar";

interface Props {
  open: boolean;
  meetingId: string;
  title: string;
  isAdmin: boolean;
  onClose: () => void;
  /** After every change, so the page can update its Share button and badges. */
  onChange: (s: Sharing) => void;
}

const LEVEL_LABEL: Record<ShareLevel, string> = { view: "Can view", edit: "Can edit" };

/** Owner-only: share a meeting with teammates or everyone on this Quill. Changes apply at once. */
export function ShareSheet({ open, meetingId, title, isAdmin, onClose, onChange }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [sharing, setSharing] = useState<Sharing | null>(null);
  const [people, setPeople] = useState<Teammate[]>([]);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setError(null);
    let live = true;
    Promise.all([api.sharing(meetingId), api.directory()]).then(
      ([s, d]) => {
        if (!live) return;
        setSharing(s);
        setPeople(d);
      },
      (e) => live && setError(errorText(e)),
    );
    return () => {
      live = false;
    };
  }, [open, meetingId]);

  const apply = async (patch: Parameters<typeof api.updateSharing>[1], done?: string) => {
    try {
      const s = await api.updateSharing(meetingId, patch);
      setSharing(s);
      onChange(s);
      if (done) toast(done);
    } catch (e) {
      toast(errorText(e), "error");
    }
  };

  const taken = useMemo(() => new Set([sharing?.owner?.id, ...(sharing?.people.map((p) => p.user_id) ?? [])]), [sharing]);
  const candidates = useMemo(() => {
    const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return people.filter((p) => !taken.has(p.id) && words.every((w) => `${p.name} ${p.email}`.toLowerCase().includes(w)));
  }, [people, taken, q]);
  const others = people.filter((p) => p.id !== sharing?.owner?.id);
  useEffect(() => setSel(0), [q]);

  const add = (p: Teammate) => {
    setQ("");
    input.current?.focus();
    void apply({ people: { [p.id]: "view" } }, `Shared with ${displayName(p)}`);
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => Math.min(candidates.length - 1, s + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(0, s - 1));
    } else if (e.key === "Enter" && q.trim() && candidates[sel]) {
      e.preventDefault();
      add(candidates[sel]);
    }
  };

  const everyone = sharing?.everyone ?? null;

  return (
    <dialog ref={ref} className="sheet-dialog share-sheet" aria-labelledby="share-title" onCancel={(e) => (e.preventDefault(), onClose())}>
      <div className="sd-body">
        <header className="sd-head">
          <span className="sd-icon" aria-hidden>
            <Users size={18} />
          </span>
          <div className="sd-title">
            <h2 id="share-title">Share meeting</h2>
            <span title={title}>{title}</span>
          </div>
          <button type="button" className="icon-btn" aria-label="Close" title="Close (Esc)" onClick={onClose}>
            <X size={16} />
          </button>
        </header>
        <div className="sd-pad share-body">
          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}
          {others.length === 0 && sharing ? (
            <div className="share-alone">
              <p>No one else is on this Quill yet.</p>
              {isAdmin ? (
                <a className="btn sm" href="/settings/people?invite=1" onClick={(e) => (onClose(), linkClick(e))}>
                  Invite someone
                </a>
              ) : (
                <span className="muted">Ask an admin to invite your teammates.</span>
              )}
            </div>
          ) : (
            <div className="share-add">
              <span className="search-field">
                <Search size={14} />
                <input
                  ref={input}
                  className="input"
                  placeholder="Add people by name or email"
                  aria-label="Add people by name or email"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  onKeyDown={onKey}
                  role="combobox"
                  aria-expanded={q.trim().length > 0 && candidates.length > 0}
                  aria-controls="share-suggest"
                  autoComplete="off"
                />
              </span>
              {q.trim() && (
                <ul className="share-suggest" id="share-suggest" role="listbox">
                  {candidates.length === 0 ? (
                    <li className="none">{others.every((p) => taken.has(p.id)) ? "Everyone here already has access." : "No one matches."}</li>
                  ) : (
                    candidates.slice(0, 6).map((p, i) => (
                      <li key={p.id} role="option" aria-selected={i === sel}>
                        <button type="button" onClick={() => add(p)} onMouseEnter={() => setSel(i)}>
                          <PersonAvatar user={p} size={26} />
                          <span className="sg-text">
                            <b>{displayName(p)}</b>
                            <span>{p.email}</span>
                          </span>
                        </button>
                      </li>
                    ))
                  )}
                </ul>
              )}
            </div>
          )}

          <div className="share-list" aria-label="People with access">
            <div className="share-h">People with access</div>
            {sharing === null && !error ? (
              <div className="skeleton" style={{ height: 52 }} />
            ) : (
              <>
                {sharing?.owner && (
                  <div className="share-row">
                    <PersonAvatar user={sharing.owner} size={30} />
                    <span className="who">
                      <b>{displayName(sharing.owner)} (you)</b>
                      <span>{sharing.owner.email}</span>
                    </span>
                    <span className="lvl">Owner</span>
                  </div>
                )}
                {sharing?.people.map((p) => (
                  <div key={p.user_id} className={`share-row ${p.disabled ? "off" : ""}`}>
                    <PersonAvatar user={p} size={30} off={p.disabled} />
                    <span className="who">
                      <b>{displayName(p)}</b>
                      <span>{p.disabled ? "Account turned off" : p.email}</span>
                    </span>
                    <select
                      className="select sm"
                      aria-label={`Access for ${displayName(p)}`}
                      value={p.access}
                      onChange={(e) => {
                        const v = e.target.value;
                        if (v === "remove") void apply({ people: { [p.user_id]: null } }, `Stopped sharing with ${displayName(p)}`);
                        else void apply({ people: { [p.user_id]: v as ShareLevel } });
                      }}
                    >
                      <option value="view">{LEVEL_LABEL.view}</option>
                      <option value="edit">{LEVEL_LABEL.edit}</option>
                      <option value="remove">Remove</option>
                    </select>
                  </div>
                ))}
              </>
            )}
          </div>

          <div className="share-general">
            <div className="share-h">Everyone on this Quill</div>
            <div className="share-row">
              <span className={`share-gico ${everyone ? "on" : ""}`} aria-hidden>
                {everyone ? <Building2 size={15} /> : <Lock size={15} />}
              </span>
              <span className="who">
                <b>{everyone ? `Everyone ${everyone === "edit" ? "can edit" : "can view"}` : "Only people added"}</b>
                <span>{everyone ? "It shows up in every teammate's library." : "Nobody else can find or open it."}</span>
              </span>
              <select
                className="select sm"
                aria-label="Access for everyone on this Quill"
                value={everyone ?? "none"}
                onChange={(e) => {
                  const v = e.target.value === "none" ? null : (e.target.value as ShareLevel);
                  void apply({ everyone: v }, v ? `Everyone on this Quill ${v === "edit" ? "can edit" : "can view"} it` : "Only people you added can open it");
                }}
              >
                <option value="none">Only people added</option>
                <option value="view">Everyone can view</option>
                <option value="edit">Everyone can edit</option>
              </select>
            </div>
          </div>
          <p className="share-note">
            <b>Can view</b>: watch, read, search and export. <b>Can edit</b>: also rename the meeting and its speakers. Only you can re-run processing, delete
            it or change sharing.
          </p>
        </div>
        <footer className="sd-foot">
          <span className="muted">Quill doesn't notify anyone. Send them the link.</span>
          <button type="button" className="btn" onClick={() => void copyText(momentUrl(meetingId), "Link copied")}>
            <Link2 size={14} /> Copy link
          </button>
          <button type="button" className="btn primary" onClick={onClose}>
            <Check size={14} /> Done
          </button>
        </footer>
      </div>
    </dialog>
  );
}
