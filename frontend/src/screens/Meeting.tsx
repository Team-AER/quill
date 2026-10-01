import { ArrowLeft, AudioLines, Copy, Download, Keyboard, Link2, MoreHorizontal, Pencil, RotateCcw, Trash2, Users, Video } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, exportUrl, frameImageUrl } from "../api/client";
import { EXPORT_FORMATS, STAGE_LABEL, type Frame, type Speaker, type User } from "../api/types";
import { AvatarStack } from "../components/Avatars";
import { Confirm } from "../components/Confirm";
import { Menu } from "../components/Menu";
import { PersonAvatar } from "../components/PersonAvatar";
import { ShareSheet } from "../components/ShareSheet";
import { StatusPill } from "../components/StatusPill";
import { LANGUAGES } from "../components/UploadSheet";
import { copyText, momentUrl, notesMarkdown } from "../lib/copy";
import { patchMeeting, removeMeeting } from "../lib/meetings";
import { displayName } from "../lib/people";
import { PlayerContext, PlayerStore } from "../lib/player";
import { linkClick, navigate } from "../lib/router";
import { meetingPhase, sortStages } from "../lib/status";
import { formatDate, formatDuration, formatTimestamp } from "../lib/time";
import { errorText, toast } from "../lib/toast";
import { isTyping, openShortcuts } from "../lib/ui";
import { Pipeline } from "./meeting/Pipeline";
import { Player, RATES } from "./meeting/Player";
import { ActionsTab, Lightbox, MomentsTab, NotesTab, SpeakersTab, SummaryTab } from "./meeting/Tabs";
import { Timeline } from "./meeting/Timeline";
import { Transcript, type TranscriptHandle } from "./meeting/Transcript";
import { useMeetingData } from "./meeting/useMeetingData";

type TabId = "summary" | "notes" | "actions" | "moments" | "speakers";

const whenFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

export function MeetingScreen({ id, user, initialT }: { id: string; user: User; initialT: number | null }) {
  const data = useMeetingData(id);
  const { meeting, lines, notes, frames, moments } = data;
  const store = useMemo(() => new PlayerStore(), []);
  const [tab, setTab] = useState<TabId>("summary");
  const [lightbox, setLightbox] = useState<number | null>(null);
  const [editingTitle, setEditingTitle] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [rerunStage, setRerunStage] = useState<{ stage: string; options?: Record<string, unknown> } | null>(null);
  const [sharing, setSharing] = useState(false);
  const [busy, setBusy] = useState(false);
  const tx = useRef<TranscriptHandle>(null);
  const seeded = useRef(false);
  const tabsRef = useRef<HTMLDivElement>(null);
  const [ink, setInk] = useState<{ left: number; width: number } | null>(null);

  const speakers = useMemo(() => new Map<string, Speaker>((meeting?.speakers ?? []).map((s) => [s.label, s])), [meeting?.speakers]);
  const isVideo = meeting ? meeting.mode !== "audio" : true;
  const tabs = useMemo(() => {
    const t: { id: TabId; label: string; count?: number; hide?: boolean }[] = [
      { id: "summary", label: "Overview" },
      { id: "notes", label: "Notes", count: notes ? notes.decisions.length + notes.open_questions.length : undefined },
      { id: "actions", label: "Action items", count: notes?.action_items.length },
      { id: "moments", label: "Key moments", count: frames.length || undefined, hide: !isVideo },
      { id: "speakers", label: "Speakers", count: speakers.size || undefined },
    ];
    return t.filter((x) => !x.hide);
  }, [notes, frames.length, isVideo, speakers.size]);

  useEffect(() => {
    if (meeting && initialT != null && !seeded.current) {
      seeded.current = true;
      store.seek(initialT);
    }
  }, [meeting, initialT, store]);

  useEffect(() => {
    if (meeting) document.title = `${meeting.title} · Quill`;
    return () => {
      document.title = "Quill";
    };
  }, [meeting?.title, meeting]);

  // Sliding underline under the selected tab.
  useLayoutEffect(() => {
    const el = tabsRef.current?.querySelector<HTMLElement>(`#tab-${tab}`);
    setInk(el ? { left: el.offsetLeft, width: el.offsetWidth } : null);
  }, [tab, tabs, meeting != null]);
  useEffect(() => {
    const el = tabsRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const b = el.querySelector<HTMLElement>(`[aria-selected="true"]`);
      if (b) setInk({ left: b.offsetLeft, width: b.offsetWidth });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [meeting != null]);

  const copyMoment = useCallback(() => {
    const t = store.time;
    void copyText(momentUrl(id, t), t >= 1 ? `Link to ${formatTimestamp(t)} copied` : "Link copied");
  }, [id, store]);

  // Keyboard shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e)) return;
      const tag = (e.target as HTMLElement)?.tagName;
      const onButton = tag === "BUTTON" || tag === "A" || (e.target as HTMLElement)?.getAttribute("role") === "slider";
      const onMedia = tag === "VIDEO" || tag === "AUDIO";
      switch (e.key) {
        case " ":
          if (onButton || onMedia) return;
          e.preventDefault();
          store.toggle();
          break;
        case "k":
          store.toggle();
          break;
        case "j":
          store.skip(-10);
          break;
        case "l":
          store.skip(10);
          break;
        case "ArrowLeft":
          if (onButton || onMedia) return;
          store.skip(-5);
          break;
        case "ArrowRight":
          if (onButton || onMedia) return;
          store.skip(5);
          break;
        case "[":
        case "]": {
          const i = RATES.indexOf(store.rate);
          const next = RATES[Math.max(0, Math.min(RATES.length - 1, (i < 0 ? 1 : i) + (e.key === "]" ? 1 : -1)))];
          store.setRate(next);
          toast(`Speed ${next}×`);
          break;
        }
        case "m":
          store.toggleMute();
          break;
        case "c":
          copyMoment();
          break;
        case "f":
          tx.current?.toggleFollow();
          break;
        case "/":
          e.preventDefault();
          tx.current?.focusSearch();
          break;
        default:
          if (/^[1-5]$/.test(e.key)) {
            const t = tabs[Number(e.key) - 1];
            if (t) setTab(t.id);
          }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [store, tabs, copyMoment]);

  const openFrame = useCallback(
    (f: Frame) => {
      store.seek(f.t);
      const i = frames.findIndex((x) => x.id === f.id);
      setLightbox(i >= 0 ? i : null);
    },
    [frames, store],
  );

  const soloSpeaker = useCallback((label: string) => tx.current?.soloSpeaker(label), []);

  const poster = useMemo(() => {
    const f = frames.find((x) => (x.relevance ?? 1) > 0) ?? frames[0];
    return f ? frameImageUrl(f) : null;
  }, [frames]);

  const doRerun = async (stage: string, options: Record<string, unknown> = {}) => {
    setBusy(true);
    try {
      await api.rerun(id, stage, options);
      toast(`${STAGE_LABEL[stage] ?? stage} queued again`);
      await data.reloadMeeting().catch(() => {});
    } catch (e) {
      toast(`Couldn't restart: ${errorText(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };

  if (data.notFound)
    return (
      <div className="page">
        <div className="page-inner empty">
          <h3>Meeting not found</h3>
          <p>It may have been deleted.</p>
          <a href="/" onClick={linkClick}>
            Back to the library
          </a>
        </div>
      </div>
    );
  if (!meeting)
    return (
      <div className="meeting" aria-busy="true">
        {data.error ? (
          <div className="page-inner">
            <div className="banner failed" role="alert">
              <span className="b-text">Couldn't load this meeting: {data.error}</span>
              <button className="btn sm" onClick={() => void data.reload()}>
                Retry
              </button>
            </div>
          </div>
        ) : (
          <>
            <header className="m-head">
              <span className="skeleton" style={{ height: 22, width: 280 }} />
            </header>
            <div className="m-body">
              <div className="m-left">
                <span className="skeleton" style={{ aspectRatio: "16/9", borderRadius: 12 }} />
                <span className="skeleton" style={{ height: 96, borderRadius: 12 }} />
              </div>
              <aside className="m-right">
                <div className="tx-skel" style={{ padding: 16 }}>
                  {[80, 62, 90, 45, 70, 84, 52].map((w, i) => (
                    <span key={i} className="skeleton" style={{ width: `${w}%` }} />
                  ))}
                </div>
              </aside>
            </div>
          </>
        )}
      </div>
    );

  const stages = sortStages(meeting.stages);
  const st = (name: string) => stages.find((s) => s.name === name);
  const pendingFor = (name: string, what: string) => {
    const s = st(name);
    if (!s) return `${what} will appear here once processing finishes.`;
    if (s.status === "skipped") return `${what} aren't generated for audio-only meetings.`;
    if (s.status === "failed") return `The ${STAGE_LABEL[name]} stage failed. Retry it from the banner above.`;
    if (s.status === "running") return `${what} are being generated (${Math.round((s.progress ?? 0) * 100)}%).`;
    if (s.status === "paused") return `${what} are waiting: ${s.detail || "paused"}.`;
    if (s.status === "done") return `No ${what.toLowerCase()} for this meeting.`;
    return `${what} will appear once the earlier stages finish.`;
  };
  const txStage = st("transcribe");
  const txHint = (() => {
    const s = txStage;
    if (!s || s.status === "pending") return "The transcript appears as soon as transcription starts.";
    if (s.status === "running") return "Transcribing… lines appear here as each part finishes.";
    if (s.status === "paused") return `Transcription paused: ${s.detail || "waiting"}.`;
    if (s.status === "failed") return "Transcription failed. Retry it from the banner above.";
    return "No speech was found in this recording.";
  })();
  const lang = LANGUAGES.find((l) => l.id === meeting.language)?.label;
  const phase = meetingPhase(meeting);
  const created = new Date(meeting.created_at);
  // Shared meetings: viewers read, editors may also rename; only the owner re-runs, deletes or shares.
  const access = meeting.access ?? "owner";
  const isOwner = access === "owner";
  const canEdit = access !== "view";
  const shared = !!meeting.everyone_access || (meeting.shared_with ?? 0) > 0;
  const shareSummary = [
    meeting.shared_with ? `Shared with ${meeting.shared_with} ${meeting.shared_with === 1 ? "person" : "people"}` : "",
    meeting.everyone_access ? `everyone on this Quill ${meeting.everyone_access === "edit" ? "can edit" : "can view"}` : "",
  ]
    .filter(Boolean)
    .join(", ");

  const saveTitle = async (title: string) => {
    setEditingTitle(false);
    const t = title.trim();
    if (!t || t === meeting.title) return;
    const prev = meeting.title;
    data.setMeeting((m) => ({ ...m, title: t }));
    try {
      await api.renameMeeting(id, t);
    } catch (e) {
      data.setMeeting((m) => ({ ...m, title: prev }));
      toast(`Rename failed: ${errorText(e)}`, "error");
    }
  };

  return (
    <PlayerContext.Provider value={store}>
      <div className="meeting">
        <header className="m-head">
          <a className="icon-btn back" href="/" onClick={linkClick} aria-label="Back to library" title="Library">
            <ArrowLeft size={16} />
          </a>
          <div className="m-title">
            {editingTitle ? (
              <input
                autoFocus
                defaultValue={meeting.title}
                aria-label="Meeting title"
                onFocus={(e) => e.currentTarget.select()}
                onBlur={(e) => void saveTitle(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void saveTitle(e.currentTarget.value);
                  if (e.key === "Escape") setEditingTitle(false);
                }}
              />
            ) : (
              <h1>
                {canEdit ? (
                  <button type="button" onClick={() => setEditingTitle(true)} title="Click to rename">
                    {meeting.title}
                  </button>
                ) : (
                  meeting.title
                )}
              </h1>
            )}
            <div className="sub">
              <span title={created.toLocaleString()}>
                {formatDate(meeting.created_at)}
                {!formatDate(meeting.created_at).startsWith("Today") && !Number.isNaN(created.getTime()) ? `, ${whenFmt.format(created)}` : ""}
              </span>
              {meeting.duration_s ? <span>{formatDuration(meeting.duration_s)}</span> : null}
              <span className="with-icon">
                {isVideo ? <Video size={12} /> : <AudioLines size={12} />}
                {isVideo ? "Video" : "Audio"}
                {lang && meeting.language !== "auto" ? ` · ${lang}` : ""}
              </span>
              {meeting.speakers && meeting.speakers.length > 0 && (
                <button type="button" className="spk-btn" onClick={() => setTab("speakers")} title="Speakers">
                  <AvatarStack speakers={meeting.speakers} max={5} size={18} />
                </button>
              )}
            </div>
          </div>
          {phase.tone !== "ok" && <StatusPill meeting={meeting} />}
          <div className="m-actions">
            {isOwner ? (
              <button
                type="button"
                className={`btn sm share-btn ${shared ? "on" : ""}`}
                onClick={() => setSharing(true)}
                title={shared ? shareSummary : "Share with teammates"}
              >
                <Users size={14} /> {shared ? "Shared" : "Share"}
                {meeting.shared_with ? <span className="seg-n">{meeting.shared_with}</span> : null}
              </button>
            ) : (
              meeting.owner && (
                <span className="shared-by" title={`${displayName(meeting.owner)} (${meeting.owner.email}) shared this with you`}>
                  <PersonAvatar user={meeting.owner} size={18} />
                  <span>
                    {displayName(meeting.owner)} · {meeting.access === "edit" ? "Can edit" : "Can view"}
                  </span>
                </span>
              )
            )}
            <button type="button" className="icon-btn" onClick={copyMoment} aria-label="Copy link to the current moment (C)" title="Copy link to the current moment (C)">
              <Link2 size={16} />
            </button>
            <Menu label="Export" button={<Download size={16} />}>
              {(close) => (
                <>
                  {notes && (
                    <>
                      <button
                        type="button"
                        onClick={() => {
                          close();
                          void copyText(notesMarkdown(meeting, notes, speakers), "Notes copied as Markdown");
                        }}
                      >
                        <Copy size={14} /> Copy notes as Markdown
                      </button>
                      <div className="sep" />
                    </>
                  )}
                  <div className="label">Download</div>
                  {EXPORT_FORMATS.map((f) => (
                    <a key={f.id} href={exportUrl(id, f.id)} download onClick={() => close()}>
                      <Download size={14} /> {f.label}
                    </a>
                  ))}
                </>
              )}
            </Menu>
            <Menu label="More actions" button={<MoreHorizontal size={16} />}>
              {(close) => (
                <>
                  {canEdit && (
                    <button
                      type="button"
                      onClick={() => {
                        close();
                        setEditingTitle(true);
                      }}
                    >
                      <Pencil size={14} /> Rename
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      close();
                      openShortcuts();
                    }}
                  >
                    <Keyboard size={14} /> Keyboard shortcuts
                  </button>
                  {isOwner && <div className="sep" />}
                  {isOwner && <div className="label">Run again from</div>}
                  {isOwner &&
                    stages
                      .filter((s) => s.status !== "skipped" && s.status !== "pending")
                      .map((s) => (
                        <button
                          key={s.name}
                          type="button"
                          disabled={busy}
                          onClick={() => {
                            close();
                            setRerunStage({ stage: s.name });
                          }}
                        >
                          <RotateCcw size={14} /> {STAGE_LABEL[s.name] ?? s.name}
                        </button>
                      ))}
                  {isOwner && <div className="sep" />}
                  {isOwner && (
                    <button
                      type="button"
                      className="danger"
                      onClick={() => {
                        close();
                        setConfirmDelete(true);
                      }}
                    >
                      <Trash2 size={14} /> Delete meeting
                    </button>
                  )}
                </>
              )}
            </Menu>
          </div>
        </header>
        <Pipeline meeting={meeting} isAdmin={user.is_admin} onRetry={isOwner ? (s) => void doRerun(s) : undefined} busy={busy} />
        <div className="m-body">
          <div className="m-left">
            <Player meeting={meeting} lines={lines} speakers={speakers} chapters={notes?.chapters ?? []} poster={poster} deepLinked={initialT != null} />
            <Timeline
              duration={meeting.duration_s ?? 0}
              lines={lines}
              speakers={speakers}
              chapters={notes?.chapters ?? []}
              frames={frames}
              moments={moments}
              onFrame={openFrame}
              onSpeaker={soloSpeaker}
            />
            <div className="tabs-wrap">
              <div className="tabs" role="tablist" aria-label="Meeting notes" ref={tabsRef}>
                {tabs.map((t, i) => (
                  <button
                    key={t.id}
                    id={`tab-${t.id}`}
                    type="button"
                    role="tab"
                    aria-selected={tab === t.id}
                    aria-controls={`panel-${t.id}`}
                    tabIndex={tab === t.id ? 0 : -1}
                    title={`${t.label} (${i + 1})`}
                    onClick={() => setTab(t.id)}
                    onKeyDown={(e) => {
                      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
                      if (!d) return;
                      e.preventDefault();
                      e.stopPropagation();
                      const next = tabs[(i + d + tabs.length) % tabs.length];
                      setTab(next.id);
                      document.getElementById(`tab-${next.id}`)?.focus();
                    }}
                  >
                    {t.label}
                    {t.count != null && t.count > 0 && <span className="count num">{t.count}</span>}
                  </button>
                ))}
                {ink && <i className="tab-ink" style={{ transform: `translateX(${ink.left}px)`, width: ink.width }} aria-hidden />}
              </div>
              {notes && (
                <button type="button" className="btn sm ghost tabs-action" onClick={() => void copyText(notesMarkdown(meeting, notes, speakers), "Notes copied as Markdown")} title="Copy the notes as Markdown">
                  <Copy size={12} /> Copy notes
                </button>
              )}
            </div>
            <div className="tabpanel" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} key={tab}>
              {tab === "summary" && (
                <SummaryTab
                  notes={notes}
                  frames={frames}
                  lines={lines}
                  speakers={speakers}
                  pendingText={pendingFor("synthesize", "Summary and chapters")}
                  onResummarize={isOwner ? () => setRerunStage({ stage: "synthesize" }) : undefined}
                  onSpeaker={soloSpeaker}
                />
              )}
              {tab === "notes" && <NotesTab notes={notes} pendingText={pendingFor("synthesize", "Decisions and questions")} />}
              {tab === "actions" && <ActionsTab meetingId={id} notes={notes} speakers={speakers} pendingText={pendingFor("synthesize", "Action items")} />}
              {tab === "moments" && <MomentsTab frames={frames} moments={moments} onOpen={openFrame} pendingText={pendingFor("frames", "Key moments")} />}
              {tab === "speakers" && (
                <SpeakersTab
                  meeting={meeting}
                  canEdit={canEdit}
                  canRerun={isOwner}
                  lines={lines}
                  speakers={speakers}
                  onRenamed={(label, name) =>
                    data.setMeeting((m) => ({
                      ...m,
                      speakers: (m.speakers ?? []).some((s) => s.label === label)
                        ? (m.speakers ?? []).map((s) => (s.label === label ? { ...s, display_name: name, suggested_name: name && name === s.suggested_name ? null : s.suggested_name } : s))
                        : [...(m.speakers ?? []), { label, display_name: name, color: null }],
                    }))
                  }
                  onMerged={(from, into) => {
                    data.setLines((ls) => ls.map((l) => (l.speaker === from ? { ...l, speaker: into } : l)));
                    data.setMeeting((m) => ({ ...m, speakers: (m.speakers ?? []).filter((s) => s.label !== from) }));
                  }}
                  onSpeakers={(list) => data.setMeeting((m) => ({ ...m, speakers: list }))}
                  onRediarize={(n) => void doRerun("diarize", n ? { expected_speakers: n } : {})}
                />
              )}
            </div>
          </div>
          <aside className="m-right" aria-label="Transcript">
            <Transcript ref={tx} meetingId={id} lines={lines} speakers={speakers} emptyHint={txHint} growing={txStage?.status === "running"} />
          </aside>
        </div>
      </div>
      <Lightbox frames={frames} index={lightbox} onIndex={setLightbox} onClose={() => setLightbox(null)} />
      {isOwner && (
        <ShareSheet
          open={sharing}
          meetingId={id}
          title={meeting.title}
          isAdmin={user.is_admin}
          onClose={() => setSharing(false)}
          onChange={(st) => {
            const patch = { everyone_access: st.everyone, shared_with: st.people.length };
            data.setMeeting((m) => ({ ...m, ...patch }));
            patchMeeting(id, patch);
          }}
        />
      )}
      <Confirm
        open={confirmDelete}
        title="Delete this meeting?"
        body="The recording, transcript, frames and notes are removed for everyone. This can't be undone."
        confirmLabel="Delete"
        danger
        onCancel={() => setConfirmDelete(false)}
        onConfirm={async () => {
          setConfirmDelete(false);
          try {
            await api.deleteMeeting(id);
            removeMeeting(id);
            toast("Meeting deleted");
            navigate("/");
          } catch (e) {
            toast(`Delete failed: ${errorText(e)}`, "error");
          }
        }}
      />
      <Confirm
        open={rerunStage != null}
        title={`Run ${rerunStage ? (STAGE_LABEL[rerunStage.stage] ?? rerunStage.stage) : ""} again?`}
        body="This stage and every stage after it run again. Current results stay visible until they're replaced."
        confirmLabel="Run again"
        onCancel={() => setRerunStage(null)}
        onConfirm={() => {
          const r = rerunStage!;
          setRerunStage(null);
          void doRerun(r.stage, r.options);
        }}
      />
    </PlayerContext.Provider>
  );
}
