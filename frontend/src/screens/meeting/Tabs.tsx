import { AlertTriangle, Check, ChevronLeft, ChevronRight, CircleHelp, Copy, GitMerge, ListChecks, Pencil, RotateCcw, Sparkles, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import { api, frameImageUrl } from "../../api/client";
import type { ActionItem, Frame, Meeting, Moment, Notes, Speaker, TranscriptLine } from "../../api/types";
import { Confirm } from "../../components/Confirm";
import { Avatar } from "../../components/Avatars";
import { Menu } from "../../components/Menu";
import { actionLines, copyText } from "../../lib/copy";
import { usePref } from "../../lib/prefs";
import { usePlayer, usePlayerValue } from "../../lib/player";
import { speakerName, speakerSlot, speakerVar } from "../../lib/speakers";
import { formatDuration, formatTimestamp } from "../../lib/time";
import { errorText, toast } from "../../lib/toast";
import { talkTime } from "../../lib/transcript";

export function TsLink({ t, label }: { t: number | null | undefined; label?: string }) {
  const store = usePlayer();
  if (t == null || !Number.isFinite(t)) return null;
  return (
    <button type="button" className="ts-link" onClick={() => store.seek(t, true)} aria-label={`${label ? label + ", " : ""}play from ${formatTimestamp(t)}`} title="Play from here">
      {formatTimestamp(t)}
    </button>
  );
}

function Ungrounded() {
  return (
    <span className="flag" title="Quill could not match this to a moment in the transcript. Double-check it before relying on it.">
      <AlertTriangle size={11} /> Not found in transcript
    </span>
  );
}

function NotReady({ text }: { text: string }) {
  return (
    <div className="empty" style={{ padding: "32px 16px" }}>
      <Sparkles size={24} />
      <p style={{ margin: 0 }}>{text}</p>
    </div>
  );
}

// ---------- Overview ----------
function TalkTime({ lines, speakers, onSpeaker }: { lines: TranscriptLine[]; speakers: Map<string, Speaker>; onSpeaker: (label: string) => void }) {
  const rows = useMemo(() => {
    const tt = talkTime(lines);
    const labels = new Set<string>([...speakers.keys(), ...tt.keys()]);
    const list = [...labels].map((l) => ({ label: l, secs: speakers.get(l)?.talk_time_s || tt.get(l) || 0 })).filter((r) => r.secs > 0);
    const total = list.reduce((a, r) => a + r.secs, 0) || 1;
    return list.sort((a, b) => b.secs - a.secs).map((r) => ({ ...r, share: r.secs / total }));
  }, [lines, speakers]);
  if (!rows.length) return null;
  return (
    <div className="ov-card talk">
      <div className="ov-head">Talk time</div>
      <div className="talk-bar" aria-hidden>
        {rows.map((r) => (
          <i key={r.label} style={{ flexGrow: r.share, background: speakerVar(speakerSlot(r.label, speakers)) }} title={`${speakerName(r.label, speakers)} · ${Math.round(r.share * 100)}%`} />
        ))}
      </div>
      <ul className="talk-legend">
        {rows.map((r) => (
          <li key={r.label}>
            <button type="button" onClick={() => onSpeaker(r.label)} title={`Show only ${speakerName(r.label, speakers)} in the transcript`}>
              <i style={{ background: speakerVar(speakerSlot(r.label, speakers)) }} />
              <span className="nm">{speakerName(r.label, speakers)}</span>
              <span className="num pc">{Math.round(r.share * 100)}%</span>
              <span className="num tm">{formatDuration(r.secs)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function SummaryTab({
  notes,
  frames,
  lines,
  speakers,
  pendingText,
  onResummarize,
  onSpeaker,
}: {
  notes: Notes | null;
  frames: Frame[];
  lines: TranscriptLine[];
  speakers: Map<string, Speaker>;
  pendingText: string;
  /** Left out when you can't re-run it (shared with you). */
  onResummarize?: () => void;
  onSpeaker: (label: string) => void;
}) {
  const cur = usePlayerValue((p) => {
    if (!notes) return -1;
    let idx = -1;
    notes.chapters.forEach((c, i) => {
      if (c.start <= p.time) idx = i;
    });
    return idx;
  });
  const chFrac = usePlayerValue((p) => {
    if (!notes || cur < 0) return 0;
    const c = notes.chapters[cur];
    const end = notes.chapters[cur + 1]?.start ?? c.end;
    return end > c.start ? Math.round(Math.min(1, Math.max(0, (p.time - c.start) / (end - c.start))) * 100) / 100 : 0;
  });
  const store = usePlayer();
  const thumbs = useMemo(() => {
    if (!notes) return [];
    const rel = frames.filter((f) => (f.relevance ?? 1) > 0);
    return notes.chapters.map((c, i) => {
      const end = notes.chapters[i + 1]?.start ?? c.end;
      return rel.find((f) => f.t >= c.start && f.t < end) ?? null;
    });
  }, [notes, frames]);
  if (!notes) return <NotReady text={pendingText} />;
  return (
    <>
      <div className="ov-grid">
        {notes.tldr.length > 0 && (
          <div className="ov-card tldr-card">
            <div className="ov-head">
              <Sparkles size={13} /> TL;DR
            </div>
            <ul className="tldr">
              {notes.tldr.map((t, i) => (
                <li key={i}>{t}</li>
              ))}
            </ul>
          </div>
        )}
        <TalkTime lines={lines} speakers={speakers} onSpeaker={onSpeaker} />
      </div>
      {notes.summary && (
        <div className="section">
          <h3>
            Summary
            <span className="spacer" />
            {onResummarize && (
              <button type="button" className="btn sm ghost" onClick={onResummarize} title="Run the Notes stage again">
                <RotateCcw size={12} /> Re-summarize
              </button>
            )}
          </h3>
          <div className="markdown">
            <Markdown>{notes.summary}</Markdown>
          </div>
        </div>
      )}
      {notes.chapters.length > 0 && (
        <div className="section">
          <h3>Chapters · {notes.chapters.length}</h3>
          <div className="chapters">
            {notes.chapters.map((c, i) => (
              <button key={i} type="button" className={`chapter ${i === cur ? "current" : ""}`} onClick={() => store.seek(c.start, true)} aria-current={i === cur ? "true" : undefined}>
                <span className="ch-thumb" aria-hidden>
                  {thumbs[i] ? <img src={frameImageUrl(thumbs[i]!, true)} alt="" loading="lazy" decoding="async" /> : <span className="ch-n num">{i + 1}</span>}
                  <span className="ch-ts num">{formatTimestamp(c.start)}</span>
                </span>
                <span className="body">
                  <span className="ctitle">{c.title}</span>
                  {c.summary && <span className="csum">{c.summary}</span>}
                  <span className="cmeta num">{formatDuration((notes.chapters[i + 1]?.start ?? c.end) - c.start)}</span>
                </span>
                {i === cur && <i className="ch-progress" style={{ width: `${chFrac * 100}%` }} aria-hidden />}
              </button>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

// ---------- Notes ----------
export function NotesTab({ notes, pendingText }: { notes: Notes | null; pendingText: string }) {
  if (!notes) return <NotReady text={pendingText} />;
  const block = (title: string, icon: React.ReactNode, items: Notes["decisions"], empty: string, tone: string) => (
    <div className={`section notes-block ${tone}`}>
      <h3>
        {icon}
        {title}
        <span className="muted">· {items.length}</span>
      </h3>
      {items.length ? (
        <div className="list">
          {items.map((d, i) => (
            <div key={i} className="list-row">
              {d.t != null ? <TsLink t={d.t} label={title} /> : <span className="chip">–:–</span>}
              <div className="body">
                {d.text}
                {!d.grounded && (
                  <div className="meta">
                    <Ungrounded />
                  </div>
                )}
              </div>
              <span />
            </div>
          ))}
        </div>
      ) : (
        <p className="muted" style={{ margin: "0 8px" }}>
          {empty}
        </p>
      )}
    </div>
  );
  return (
    <>
      {block("Decisions", <Check size={12} />, notes.decisions, "No decisions were recorded.", "ok")}
      {block("Open questions and risks", <CircleHelp size={12} />, notes.open_questions, "No open questions.", "attn")}
    </>
  );
}

// ---------- Action items ----------
function Owner({ owner, speakers }: { owner: string | null; speakers: Map<string, Speaker> }) {
  if (!owner) return <span className="owner muted">Unassigned</span>;
  const isLabel = /^S\d+$/.test(owner);
  if (isLabel) {
    return (
      <span className="owner">
        <Avatar label={owner} speakers={speakers} size={16} />
        {speakerName(owner, speakers)}
      </span>
    );
  }
  // A name: colour it if it matches a speaker's display name
  let match: Speaker | undefined;
  speakers.forEach((s) => {
    if (s.display_name && s.display_name.toLowerCase().startsWith(owner.toLowerCase())) match = s;
  });
  if (match) {
    return (
      <span className="owner">
        <Avatar label={match.label} speakers={speakers} size={16} />
        {owner}
      </span>
    );
  }
  return <span className="owner">{owner}</span>;
}

function formatDue(due: string | null): string | null {
  if (!due) return null;
  const d = new Date(due);
  if (Number.isNaN(d.getTime())) return due;
  return new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: d.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined }).format(d);
}

export function ActionsTab({ meetingId, notes, speakers, pendingText }: { meetingId: string; notes: Notes | null; speakers: Map<string, Speaker>; pendingText: string }) {
  const [doneList, setDoneList] = usePref<string[]>(`done.${meetingId}`, []);
  const done = useMemo(() => new Set(doneList), [doneList]);
  if (!notes) return <NotReady text={pendingText} />;
  const items: ActionItem[] = notes.action_items;
  if (!items.length) return <NotReady text="No action items were found in this meeting." />;
  const toggle = (task: string) => setDoneList(done.has(task) ? doneList.filter((t) => t !== task) : [...doneList, task]);
  const count = items.filter((a) => done.has(a.task)).length;
  return (
    <div className="section">
      <h3>
        <ListChecks size={12} /> Action items <span className="muted">· {count ? `${count} of ${items.length} done` : items.length}</span>
        {items.some((a) => !a.grounded) && (
          <span className="muted" style={{ fontWeight: 400 }}>
            · {items.filter((a) => !a.grounded).length} need checking
          </span>
        )}
        <span className="spacer" />
        <button type="button" className="btn sm ghost" onClick={() => void copyText(actionLines(notes, speakers, done).join("\n"), "Checklist copied")} title="Copy as a Markdown checklist">
          <Copy size={12} /> Copy checklist
        </button>
      </h3>
      <div className="list actions">
        {items.map((a, i) => (
          <div key={i} className={`list-row action ${done.has(a.task) ? "done" : ""}`}>
            <label className="check">
              <input type="checkbox" checked={done.has(a.task)} onChange={() => toggle(a.task)} aria-label={`Done: ${a.task}`} />
              <span aria-hidden>
                <Check size={11} strokeWidth={3} />
              </span>
            </label>
            <div className="body">
              <div className="task">{a.task}</div>
              <div className="meta">
                <Owner owner={a.owner} speakers={speakers} />
                {a.due && <span className="due">Due {formatDue(a.due)}</span>}
                {!a.grounded && <Ungrounded />}
              </div>
            </div>
            {a.t != null ? <TsLink t={a.t} label="Action item" /> : <span />}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------- Key moments ----------
export function MomentsTab({ frames, moments, onOpen, pendingText }: { frames: Frame[]; moments: Moment[]; onOpen: (f: Frame) => void; pendingText: string }) {
  const relevant = frames.filter((f) => (f.relevance ?? 1) > 0);
  const other = frames.filter((f) => (f.relevance ?? 1) === 0);
  const textOnly = moments.filter((m) => !frames.some((f) => f.moment_id === m.id || Math.abs(f.t - m.t) < 1));
  if (!frames.length && !moments.length) return <NotReady text={pendingText} />;
  const card = (f: Frame) => (
    <button key={f.id} type="button" className={`frame-card ${(f.relevance ?? 1) === 0 ? "low" : ""}`} onClick={() => onOpen(f)} aria-label={`Open frame at ${formatTimestamp(f.t)}: ${f.caption ?? f.title ?? ""}`}>
      <span className="thumb">
        <img src={frameImageUrl(f, true)} alt="" loading="lazy" decoding="async" />
        <span className="ts num">{formatTimestamp(f.t)}</span>
        {f.kind && <span className="kind">{f.kind}</span>}
      </span>
      <span className="cap">{f.caption || f.title || "Frame"}</span>
    </button>
  );
  return (
    <>
      {relevant.length > 0 && (
        <div className="section">
          <h3>
            Frames <span className="muted">· {relevant.length}</span>
          </h3>
          <div className="gallery">{relevant.map(card)}</div>
        </div>
      )}
      {textOnly.length > 0 && (
        <div className="section">
          <h3>Moments without a frame</h3>
          <div className="list">
            {textOnly.map((m) => (
              <div key={m.id} className="list-row">
                <TsLink t={m.t} label="Key moment" />
                <div className="body">
                  {m.why || "Key moment"}
                  {m.source && <div className="meta">{m.source}</div>}
                </div>
                <span />
              </div>
            ))}
          </div>
        </div>
      )}
      {other.length > 0 && (
        <div className="section">
          <h3>Low relevance</h3>
          <div className="gallery">{other.map(card)}</div>
        </div>
      )}
    </>
  );
}

export function Lightbox({ frames, index, onIndex, onClose }: { frames: Frame[]; index: number | null; onIndex: (i: number) => void; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const open = index != null && frames[index] != null;
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  const f = open ? frames[index!] : null;
  return (
    <dialog
      ref={ref}
      className="lightbox"
      aria-label="Frame"
      onClose={onClose}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
      onKeyDown={(e) => {
        if (index == null) return;
        if (e.key === "ArrowLeft" && index > 0) onIndex(index - 1);
        if (e.key === "ArrowRight" && index < frames.length - 1) onIndex(index + 1);
      }}
    >
      {f && (
        <div className="lb">
          <div className="lb-img">
            <img src={frameImageUrl(f)} alt={f.caption ?? f.title ?? "Frame"} />
            {index! > 0 && (
              <button type="button" className="icon-btn lb-nav prev" onClick={() => onIndex(index! - 1)} aria-label="Previous frame (←)">
                <ChevronLeft size={18} />
              </button>
            )}
            {index! < frames.length - 1 && (
              <button type="button" className="icon-btn lb-nav next" onClick={() => onIndex(index! + 1)} aria-label="Next frame (→)">
                <ChevronRight size={18} />
              </button>
            )}
          </div>
          <div className="lb-side">
            <div className="top">
              <TsLink t={f.t} label="Frame" />
              {f.kind && <span className="chip" style={{ textTransform: "capitalize" }}>{f.kind}</span>}
              {f.relevance != null && <span className="chip">Relevance {f.relevance}/3</span>}
              <span className="spacer" />
              <span className="muted num" style={{ fontSize: 11 }}>
                {index! + 1} / {frames.length}
              </span>
              <button type="button" className="icon-btn" onClick={onClose} aria-label="Close (Esc)" autoFocus>
                <X size={16} />
              </button>
            </div>
            {f.title && <h2>{f.title}</h2>}
            {f.caption && <p style={{ margin: 0 }}>{f.caption}</p>}
            {f.key_facts.length > 0 && (
              <div>
                <div className="field">
                  <span>Key facts</span>
                </div>
                <ul>
                  {f.key_facts.map((k, i) => (
                    <li key={i}>{k}</li>
                  ))}
                </ul>
              </div>
            )}
            {f.visible_text && (
              <div className="field">
                <span>Text on screen</span>
                <pre>{f.visible_text}</pre>
              </div>
            )}
          </div>
        </div>
      )}
    </dialog>
  );
}

// ---------- Speakers ----------
interface SpeakersProps {
  meeting: Meeting;
  lines: TranscriptLine[];
  speakers: Map<string, Speaker>;
  onRenamed: (label: string, name: string | null) => void;
  onMerged: (from: string, into: string) => void;
  /** Server's authoritative speakers list after a rename or merge. */
  onSpeakers: (list: Speaker[]) => void;
  onRediarize: (n: number | null) => void;
  /** Rename and merge speakers (owner or edit access). */
  canEdit: boolean;
  /** Re-run speaker detection (owner only). */
  canRerun: boolean;
}

export function SpeakersTab({ meeting, lines, speakers, onRenamed, onMerged, onSpeakers, onRediarize, canEdit, canRerun }: SpeakersProps) {
  const tt = useMemo(() => talkTime(lines), [lines]);
  const total = useMemo(() => {
    let sum = 0;
    const labels = new Set<string>([...speakers.keys(), ...tt.keys()]);
    labels.forEach((l) => (sum += speakers.get(l)?.talk_time_s || tt.get(l) || 0));
    return sum || 1;
  }, [speakers, tt]);
  const counts = useMemo(() => {
    const c = new Map<string, number>();
    lines.forEach((l) => c.set(l.speaker, (c.get(l.speaker) ?? 0) + 1));
    return c;
  }, [lines]);
  const labels = useMemo(() => {
    const s = new Set<string>(speakers.keys());
    lines.forEach((l) => s.add(l.speaker));
    return [...s].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  }, [lines, speakers]);
  const [editing, setEditing] = useState<string | null>(null);
  const [merge, setMerge] = useState<{ from: string; into: string } | null>(null);
  const [n, setN] = useState<string>(meeting.expected_speakers ? String(meeting.expected_speakers) : "");
  const [confirmRe, setConfirmRe] = useState(false);

  const save = async (label: string, name: string | null) => {
    const prev = speakers.get(label)?.display_name ?? null;
    setEditing(null);
    if ((name ?? "") === (prev ?? "")) return;
    onRenamed(label, name);
    try {
      const list = await api.renameSpeaker(meeting.id, label, name);
      if (list.length) onSpeakers(list);
      toast(name ? `Renamed to ${name}` : "Name cleared", "info", {
        label: "Undo",
        run: () => {
          onRenamed(label, prev);
          void api
            .renameSpeaker(meeting.id, label, prev)
            .then((l) => l.length && onSpeakers(l))
            .catch(() => {});
        },
      });
    } catch (e) {
      onRenamed(label, prev);
      toast(`Rename failed: ${errorText(e)}`, "error");
    }
  };

  if (!labels.length) return <NotReady text="Speakers appear once diarization finishes." />;

  return (
    <>
      <div className="section">
        <h3>
          Speakers <span className="muted">· {labels.length}</span>
          {labels.length >= 8 && <span className="flag">All 8 speaker slots used; similar voices may be merged</span>}
        </h3>
        <div>
          {labels.map((label) => {
            const sp = speakers.get(label);
            const slot = speakerSlot(label, speakers);
            const name = speakerName(label, speakers);
            const secs = sp?.talk_time_s || tt.get(label) || 0;
            return (
              <div key={label} className="speaker-row">
                <Avatar label={label} speakers={speakers} size={36} />
                <div style={{ minWidth: 0 }}>
                  <div className="name">
                    {!canEdit ? (
                      <span className="edit">{name}</span>
                    ) : editing === label ? (
                      <NameInput initial={sp?.display_name ?? ""} placeholder={name} onDone={(v) => void save(label, v)} onCancel={() => setEditing(null)} />
                    ) : (
                      <>
                        <button type="button" className="edit" onClick={() => setEditing(label)} title="Rename">
                          {name}
                        </button>
                        <button type="button" className="icon-btn sm" onClick={() => setEditing(label)} aria-label={`Rename ${name}`} title="Rename">
                          <Pencil size={12} />
                        </button>
                      </>
                    )}
                  </div>
                  <div className="meta">
                    <span className="chip">{label}</span>
                    <span className="num">
                      {formatDuration(secs)} · {Math.round((secs / total) * 100)}%
                    </span>
                    <span className="num">{counts.get(label) ?? 0} lines</span>
                    {canEdit && sp?.suggested_name && sp.suggested_name !== sp.display_name && (
                      <span className="suggest">
                        <Sparkles size={11} /> {sp.suggested_name}?
                        {sp.suggestion_evidence_t != null && <TsLink t={sp.suggestion_evidence_t} label="Evidence" />}
                        <button type="button" className="btn sm primary" onClick={() => void save(label, sp.suggested_name!)}>
                          Accept
                        </button>
                      </span>
                    )}
                  </div>
                  <div className="talkbar" aria-hidden>
                    <i style={{ width: `${(secs / total) * 100}%`, background: speakerVar(slot) }} />
                  </div>
                </div>
                {canEdit && (
                  <Menu label={`Merge ${name} into another speaker`} button={<GitMerge size={16} />}>
                    {(close) => (
                      <>
                        <div className="label">Merge {name} into…</div>
                        {labels
                          .filter((l) => l !== label)
                          .map((l) => (
                            <button
                              key={l}
                              type="button"
                              onClick={() => {
                                close();
                                setMerge({ from: label, into: l });
                              }}
                            >
                              <i style={{ width: 8, height: 8, borderRadius: "50%", background: speakerVar(speakerSlot(l, speakers)) }} />
                              {speakerName(l, speakers)}
                            </button>
                          ))}
                      </>
                    )}
                  </Menu>
                )}
              </div>
            );
          })}
        </div>
      </div>
      {canRerun && (
        <div className="section">
          <h3>Wrong number of speakers?</h3>
          <div className="inline-form">
            <label className="field" style={{ flex: "0 1 200px" }}>
              <span>Detect again with</span>
              <select className="select" value={n} onChange={(e) => setN(e.target.value)}>
                <option value="">Automatic count</option>
                {[1, 2, 3, 4, 5, 6, 7, 8].map((k) => (
                  <option key={k} value={k}>
                    {k} speakers
                  </option>
                ))}
              </select>
            </label>
            <button type="button" className="btn" onClick={() => setConfirmRe(true)}>
              <RotateCcw size={13} /> Re-run speaker detection
            </button>
          </div>
        </div>
      )}
      <Confirm
        open={merge != null}
        title="Merge speakers?"
        body={merge ? `Every line from ${speakerName(merge.from, speakers)} will be attributed to ${speakerName(merge.into, speakers)}. This can't be undone, but you can re-run speaker detection.` : ""}
        confirmLabel="Merge"
        onCancel={() => setMerge(null)}
        onConfirm={async () => {
          const m = merge!;
          setMerge(null);
          try {
            const list = await api.mergeSpeakers(meeting.id, m.from, m.into);
            onMerged(m.from, m.into);
            if (list.length) onSpeakers(list);
            toast(`Merged into ${speakerName(m.into, speakers)}`);
          } catch (e) {
            toast(`Merge failed: ${errorText(e)}`, "error");
          }
        }}
      />
      <Confirm
        open={confirmRe}
        title="Re-run speaker detection?"
        body="Quill re-detects speakers, then re-transcribes and rewrites the notes. Names you set are kept where the speaker labels match. This takes a while for long meetings."
        confirmLabel="Re-run"
        onCancel={() => setConfirmRe(false)}
        onConfirm={() => {
          setConfirmRe(false);
          onRediarize(n ? Number(n) : null);
        }}
      />
    </>
  );
}

function NameInput({ initial, placeholder, onDone, onCancel }: { initial: string; placeholder: string; onDone: (v: string | null) => void; onCancel: () => void }) {
  const [v, setV] = useState(initial);
  const done = useRef(false);
  const finish = (val: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(val);
  };
  return (
    <input
      className="input"
      style={{ height: 26, maxWidth: 260 }}
      autoFocus
      value={v}
      placeholder={placeholder}
      aria-label="Speaker name"
      onChange={(e) => setV(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") finish(v.trim() || null);
        if (e.key === "Escape") {
          done.current = true;
          onCancel();
        }
      }}
      onBlur={() => finish(v.trim() || null)}
    />
  );
}
