import { AlertTriangle, Download, FileAudio, FileVideo, LayoutGrid, Link2, List, MoreHorizontal, Pause, Play, Plus, RotateCcw, Search, Trash2, Upload, Users, X } from "lucide-react";
import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { api, exportUrl } from "../api/client";
import { STAGE_LABEL, type Meeting } from "../api/types";
import { AvatarStack } from "../components/Avatars";
import { Confirm } from "../components/Confirm";
import { Cover } from "../components/Cover";
import { Menu } from "../components/Menu";
import { StatusPill } from "../components/StatusPill";
import { copyText, momentUrl } from "../lib/copy";
import { groupByDay } from "../lib/groups";
import { removeMeeting, useMeetings } from "../lib/meetings";
import { displayName } from "../lib/people";
import { usePref } from "../lib/prefs";
import { linkClick, navigate } from "../lib/router";
import { meetingPhase, sortStages, stageEta } from "../lib/status";
import { formatBytes, formatDate, formatDuration, formatTimestamp } from "../lib/time";
import { errorText, toast } from "../lib/toast";
import { openFilePicker } from "../lib/ui";
import {
  cancelUpload,
  discardInterrupted,
  pauseUpload,
  refreshInterrupted,
  resumeUpload,
  useInterrupted,
  useUploads,
  type UploadItem,
} from "../lib/uploads";

type Filter = "all" | "processing" | "ready" | "shared";

const isMine = (m: Meeting) => (m.access ?? "owner") === "owner";
type Layout = "grid" | "list";

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" });
function when(iso: string, group: string) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return group === "today" || group === "yesterday" ? timeFmt.format(d) : group === "week" ? dayFmt.format(d) : formatDate(iso);
}

function UploadRow({ u }: { u: UploadItem }) {
  const pct = u.size ? (u.sent / u.size) * 100 : 0;
  const video = /\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(u.name);
  return (
    <div className={`upload ${u.status}`}>
      <div className="u-icon" aria-hidden>
        {video ? <FileVideo size={16} /> : <FileAudio size={16} />}
      </div>
      <div className="u-name" title={u.name}>
        {u.name}
      </div>
      <div className="u-actions">
        {u.status === "uploading" || u.status === "starting" ? (
          <button type="button" className="icon-btn" aria-label={`Pause ${u.name}`} title="Pause" onClick={() => pauseUpload(u.key)}>
            <Pause size={16} />
          </button>
        ) : u.status === "paused" || u.status === "error" ? (
          <button type="button" className="icon-btn" aria-label={`Resume ${u.name}`} title="Resume" onClick={() => resumeUpload(u.key)}>
            {u.status === "error" ? <RotateCcw size={16} /> : <Play size={16} />}
          </button>
        ) : null}
        {u.status !== "done" && (
          <button type="button" className="icon-btn" aria-label={`Cancel ${u.name}`} title="Cancel upload" onClick={() => void cancelUpload(u.key)}>
            <X size={16} />
          </button>
        )}
      </div>
      <div
        className={`progress ${u.status === "paused" ? "paused" : u.status === "error" ? "failed" : u.status === "done" ? "done" : "run"}`}
        role="progressbar"
        aria-label={`${u.name} upload`}
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <i style={{ width: `${pct}%` }} />
      </div>
      <div className="u-meta">
        <span className="num">
          {formatBytes(u.sent)} of {formatBytes(u.size)} · {Math.floor(pct)}%
        </span>
        {u.status === "uploading" && u.speed > 0 && <span className="num">{formatBytes(u.speed)}/s</span>}
        {u.status === "uploading" && u.eta != null && <span className="num">{formatDuration(u.eta)} left</span>}
        {u.status === "starting" && <span>Starting…</span>}
        {u.status === "paused" && <span>Paused</span>}
        {u.resumed && u.status !== "done" && <span>Resumed</span>}
        {u.status === "done" && (
          <span>
            Uploaded.{" "}
            {u.meetingId && (
              <a href={`/m/${u.meetingId}`} onClick={linkClick}>
                Open meeting
              </a>
            )}
          </span>
        )}
        {u.status === "error" && <span className="u-err">{u.error}</span>}
      </div>
    </div>
  );
}

function Uploads() {
  const uploads = useUploads();
  const interrupted = useInterrupted();
  useEffect(() => {
    void refreshInterrupted();
  }, []);
  if (!uploads.length && !interrupted.length) return null;
  return (
    <div className="uploads" aria-live="polite">
      {uploads.map((u) => (
        <UploadRow key={u.key} u={u} />
      ))}
      {interrupted.map((it) => (
        <div key={it.urlStorageKey} className="upload interrupted">
          <div className="u-icon" aria-hidden>
            <RotateCcw size={16} />
          </div>
          <div className="u-name">{it.name}</div>
          <div className="u-actions">
            <button type="button" className="icon-btn" title="Discard partial upload" aria-label={`Discard partial upload of ${it.name}`} onClick={() => void discardInterrupted(it)}>
              <X size={16} />
            </button>
          </div>
          <div className="u-meta">
            Interrupted {formatDate(it.creationTime)}
            {it.size ? ` · ${formatBytes(it.size)}` : ""}. Drop the same file again to continue where it stopped.
          </div>
        </div>
      ))}
    </div>
  );
}

function StagePips({ m }: { m: Meeting }) {
  const p = meetingPhase(m);
  const stages = sortStages(m.stages).filter((s) => s.status !== "skipped");
  if (!stages.length)
    return (
      <span className={`progress thin ${p.tone === "run" ? "run" : p.tone}`} aria-hidden>
        <i style={{ width: `${Math.max(3, p.overall * 100)}%` }} />
      </span>
    );
  return (
    <span className="pips" aria-hidden>
      {stages.map((s) => (
        <i key={s.name} className={s.status} title={STAGE_LABEL[s.name] ?? s.name}>
          {(s.status === "running" || s.status === "paused") && <b style={{ width: `${(s.progress ?? 0) * 100}%` }} />}
        </i>
      ))}
    </span>
  );
}

function Ring({ value }: { value: number }) {
  const c = 2 * Math.PI * 16;
  return (
    <svg viewBox="0 0 40 40" className="ring" aria-hidden>
      <circle cx="20" cy="20" r="16" className="ring-bg" />
      <circle cx="20" cy="20" r="16" className="ring-fg" strokeDasharray={`${Math.max(0.02, value) * c} ${c}`} />
    </svg>
  );
}

function CardMenu({ m, onDelete }: { m: Meeting; onDelete: () => void }) {
  return (
    <Menu label={`Actions for ${m.title}`} button={<MoreHorizontal size={16} />} buttonClass="icon-btn card-menu">
      {(close) => (
        <>
          <button
            type="button"
            onClick={() => {
              close();
              void copyText(momentUrl(m.id), "Link copied");
            }}
          >
            <Link2 size={14} /> Copy link
          </button>
          <a href={exportUrl(m.id, "md")} download onClick={() => close()}>
            <Download size={14} /> Notes (Markdown)
          </a>
          <a href={exportUrl(m.id, "txt")} download onClick={() => close()}>
            <Download size={14} /> Transcript (TXT)
          </a>
          {isMine(m) && (
            <>
              <div className="sep" />
              <button
                type="button"
                className="danger"
                onClick={() => {
                  close();
                  onDelete();
                }}
              >
                <Trash2 size={14} /> Delete…
              </button>
            </>
          )}
        </>
      )}
    </Menu>
  );
}

function useRetry(m: Meeting) {
  const [busy, setBusy] = useState(false);
  const stage = meetingPhase(m).stage?.name;
  const run = async () => {
    if (!stage) return;
    setBusy(true);
    try {
      await api.rerun(m.id, stage);
      toast(`${STAGE_LABEL[stage] ?? stage} queued again`);
    } catch (e) {
      toast(`Couldn't restart: ${errorText(e)}`, "error");
    } finally {
      setBusy(false);
    }
  };
  return { busy, run, can: Boolean(stage) };
}

function StatusNote({ m }: { m: Meeting }) {
  const p = meetingPhase(m);
  const retry = useRetry(m);
  if (p.tone === "paused")
    return (
      <div className="note paused">
        <Pause size={13} />
        <span>
          <b>{p.stage ? `${STAGE_LABEL[p.stage.name] ?? p.stage.name} paused` : "Paused"}</b>
          {p.reason ? ` · ${p.reason}` : ""}
        </span>
      </div>
    );
  if (p.tone === "failed")
    return (
      <div className="note failed">
        <AlertTriangle size={13} />
        <span>
          <b>{p.stage ? `${STAGE_LABEL[p.stage.name] ?? p.stage.name} failed` : "Failed"}</b>
          {p.reason ? ` · ${p.reason}` : ""}
        </span>
        {retry.can && isMine(m) && (
          <button type="button" className="btn sm" onClick={() => void retry.run()} disabled={retry.busy}>
            Retry
          </button>
        )}
      </div>
    );
  return null;
}

/** On the cover: who shared it with you, or that you shared it. */
function SharedBadge({ m }: { m: Meeting }) {
  if (!isMine(m) && m.owner)
    return (
      <span className="cover-shared" title={`Shared with you by ${displayName(m.owner)} · ${m.access === "edit" ? "can edit" : "can view"}`}>
        <Users size={11} />
        <span>{displayName(m.owner)}</span>
      </span>
    );
  if (isMine(m) && (m.everyone_access || m.shared_with))
    return (
      <span className="cover-shared mine" title={m.everyone_access ? "Shared with everyone on this Quill" : `Shared with ${m.shared_with} ${m.shared_with === 1 ? "person" : "people"}`}>
        <Users size={11} />
        <span>Shared</span>
      </span>
    );
  return null;
}

function MeetingCard({ m, group, i, onDelete }: { m: Meeting; group: string; i: number; onDelete: () => void }) {
  const p = meetingPhase(m);
  const eta = p.stage ? stageEta(p.stage) : null;
  const speakers = m.speakers ?? [];
  const busy = p.tone === "run" || p.tone === "neutral";
  return (
    <article className={`card tone-${p.tone}`} style={{ "--i": Math.min(i, 14) } as CSSProperties}>
      <a className="card-link" href={`/m/${m.id}`} onClick={linkClick} aria-label={m.title} />
      <Cover m={m}>
        <SharedBadge m={m} />
        {m.duration_s ? <span className="cover-dur num">{formatTimestamp(m.duration_s)}</span> : null}
        {busy && (
          <span className="cover-busy">
            <Ring value={p.overall} />
            <span>
              {p.label}
              {p.tone === "run" && p.stage?.progress != null ? ` · ${Math.round(p.stage.progress * 100)}%` : ""}
            </span>
          </span>
        )}
      </Cover>
      <CardMenu m={m} onDelete={onDelete} />
      <div className="card-body">
        <h3 className="title">{m.title}</h3>
        <div className="meta">
          {speakers.length > 0 && <AvatarStack speakers={speakers} max={4} size={18} />}
          <span>{when(m.created_at, group)}</span>
          {busy && eta != null ? (
            <span>· about {formatDuration(eta)} left</span>
          ) : p.tone === "ok" && m.action_count ? (
            <span>
              · {m.action_count} action item{m.action_count === 1 ? "" : "s"}
            </span>
          ) : null}
        </div>
        {busy ? <StagePips m={m} /> : p.tone === "ok" ? m.gist ? <p className="gist">{m.gist}</p> : null : <StatusNote m={m} />}
      </div>
    </article>
  );
}

function MeetingRow({ m, group, onDelete }: { m: Meeting; group: string; onDelete: () => void }) {
  const p = meetingPhase(m);
  return (
    <article className={`row-item tone-${p.tone}`}>
      <a className="card-link" href={`/m/${m.id}`} onClick={linkClick} aria-label={m.title} />
      <Cover m={m}>
        <SharedBadge m={m} />
      </Cover>
      <div className="row-main">
        <h3 className="title">{m.title}</h3>
        {p.tone === "ok" ? (
          m.gist && <p className="gist">{m.gist}</p>
        ) : p.tone === "run" || p.tone === "neutral" ? (
          <span className="row-prog">
            <StagePips m={m} />
            <span className="num">
              {p.label}
              {p.tone === "run" ? ` · ${Math.round(p.overall * 100)}%` : ""}
            </span>
          </span>
        ) : (
          <StatusNote m={m} />
        )}
      </div>
      <div className="row-spk">{m.speakers?.length ? <AvatarStack speakers={m.speakers} max={4} size={20} /> : null}</div>
      <span className="row-when">{when(m.created_at, group)}</span>
      <span className="row-dur num">{m.duration_s ? formatDuration(m.duration_s) : "–"}</span>
      <span className="row-status">{p.tone === "ok" ? null : <StatusPill meeting={m} />}</span>
      <CardMenu m={m} onDelete={onDelete} />
    </article>
  );
}

export function LibraryScreen() {
  const { meetings, loaded, error, reload } = useMeetings();
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [layout, setLayout] = usePref<Layout>("layout", "grid");
  const [doomed, setDoomed] = useState<Meeting | null>(null);

  const counts = useMemo(() => {
    let busy = 0;
    let secs = 0;
    let shared = 0;
    for (const m of meetings) {
      if (meetingPhase(m).tone !== "ok") busy++;
      if (!isMine(m)) shared++;
      secs += m.duration_s ?? 0;
    }
    return { busy, secs, shared };
  }, [meetings]);
  // Nothing shared with you any more: don't leave an empty filter selected.
  useEffect(() => {
    if (filter === "shared" && counts.shared === 0) setFilter("all");
  }, [filter, counts.shared]);

  const shown = useMemo(() => {
    const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return meetings.filter((m) => {
      const hay = `${m.title} ${m.gist ?? ""} ${(m.speakers ?? []).map((s) => s.display_name ?? "").join(" ")}`.toLowerCase();
      if (words.some((w) => !hay.includes(w))) return false;
      const tone = meetingPhase(m).tone;
      if (filter === "ready") return tone === "ok";
      if (filter === "processing") return tone !== "ok";
      if (filter === "shared") return !isMine(m);
      return true;
    });
  }, [meetings, q, filter]);
  const groups = useMemo(() => groupByDay(shown, (m) => m.created_at), [shown]);
  const showDropTile = filter === "all" && !q.trim() && layout === "grid";

  const del = async (m: Meeting) => {
    setDoomed(null);
    try {
      await api.deleteMeeting(m.id);
      removeMeeting(m.id);
      toast(`Deleted “${m.title}”`);
    } catch (e) {
      toast(`Delete failed: ${errorText(e)}`, "error");
    }
  };

  let n = 0;
  return (
    <div className="page library">
      <div className="page-inner wide">
        <div className="page-head">
          <div className="ph-title">
            <h1>Library</h1>
            {loaded && meetings.length > 0 && (
              <span className="sub">
                {meetings.length} meeting{meetings.length === 1 ? "" : "s"} · {formatDuration(counts.secs)} recorded
              </span>
            )}
          </div>
          <span className="spacer" />
          <form
            className="search-field"
            role="search"
            onSubmit={(e) => {
              e.preventDefault();
              if (q.trim() && !shown.length) navigate(`/search?q=${encodeURIComponent(q.trim())}`);
            }}
          >
            <Search size={14} />
            <input className="input" placeholder="Filter meetings" aria-label="Filter meetings by title, summary or speaker" value={q} onChange={(e) => setQ(e.target.value)} />
          </form>
          <div className="segmented" role="radiogroup" aria-label="Show">
            {(counts.shared ? (["all", "processing", "ready", "shared"] as const) : (["all", "processing", "ready"] as const)).map((f) => (
              <button key={f} type="button" role="radio" aria-checked={filter === f} onClick={() => setFilter(f)}>
                {f === "all" ? "All" : f === "processing" ? "In progress" : f === "ready" ? "Ready" : "Shared with me"}
                {f === "processing" && counts.busy > 0 && <span className="seg-n">{counts.busy}</span>}
                {f === "shared" && <span className="seg-n">{counts.shared}</span>}
              </button>
            ))}
          </div>
          <div className="segmented icons" role="radiogroup" aria-label="Layout">
            <button type="button" role="radio" aria-checked={layout === "grid"} aria-label="Grid" title="Grid" onClick={() => setLayout("grid")}>
              <LayoutGrid size={14} />
            </button>
            <button type="button" role="radio" aria-checked={layout === "list"} aria-label="List" title="List" onClick={() => setLayout("list")}>
              <List size={14} />
            </button>
          </div>
          <button type="button" className="btn primary" onClick={openFilePicker} title="Upload a recording (U)">
            <Plus size={15} /> New meeting
          </button>
        </div>
        <Uploads />
        {error && (
          <div className="banner failed" role="alert">
            <span className="b-text">Couldn't load meetings: {error}</span>
            <button className="btn sm" onClick={() => void reload()}>
              Retry
            </button>
          </div>
        )}
        {!loaded ? (
          <div className="cards" aria-busy="true">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="card skel">
                <span className="skeleton" style={{ aspectRatio: "16/9" }} />
                <span className="skeleton" style={{ height: 14, width: "70%" }} />
                <span className="skeleton" style={{ height: 10, width: "45%" }} />
              </div>
            ))}
          </div>
        ) : meetings.length === 0 ? (
          <button type="button" className="first-drop" onClick={openFilePicker}>
            <span className="fd-icon">
              <Upload size={26} />
            </span>
            <strong>Drop your first recording</strong>
            <span>Video or audio up to 10 GB. Quill finds who spoke, writes the transcript, picks out key slides and drafts the notes.</span>
            <span className="btn primary">Choose files…</span>
          </button>
        ) : shown.length === 0 ? (
          <div className="empty">
            <Search size={28} />
            <h3>Nothing matches</h3>
            <p>
              {q.trim() ? (
                <>
                  No meeting titles match “{q.trim()}”.{" "}
                  <a href={`/search?q=${encodeURIComponent(q.trim())}`} onClick={linkClick}>
                    Search inside transcripts instead
                  </a>
                </>
              ) : (
                "Try a different filter."
              )}
            </p>
          </div>
        ) : (
          groups.map((g, gi) => (
            <section key={g.key} className="lib-group" aria-labelledby={`g-${g.key}`}>
              <div className="grp-head">
                <h2 id={`g-${g.key}`}>{g.label}</h2>
                <span>{g.items.length}</span>
              </div>
              {layout === "grid" ? (
                <div className="cards">
                  {g.items.map((m) => (
                    <MeetingCard key={m.id} m={m} group={g.key} i={n++} onDelete={() => setDoomed(m)} />
                  ))}
                  {showDropTile && gi === 0 && (
                    <button type="button" className="card drop-tile" onClick={openFilePicker} style={{ "--i": Math.min(n++, 14) } as CSSProperties}>
                      <span className="dt-cover">
                        <span className="dt-icon">
                          <Upload size={18} />
                        </span>
                        <b>Drop a recording</b>
                      </span>
                      <span className="card-body">
                        <span className="title">Video or audio, up to 10 GB</span>
                        <span className="gist">Drop anywhere in Quill, or press U. Uploads resume if the connection drops.</span>
                      </span>
                    </button>
                  )}
                </div>
              ) : (
                <div className="rows">
                  {g.items.map((m) => (
                    <MeetingRow key={m.id} m={m} group={g.key} onDelete={() => setDoomed(m)} />
                  ))}
                </div>
              )}
            </section>
          ))
        )}
      </div>
      <Confirm
        open={doomed != null}
        title="Delete this meeting?"
        body={doomed ? `“${doomed.title}”: the recording, transcript, frames and notes are removed for everyone. This can't be undone.` : ""}
        confirmLabel="Delete"
        danger
        onCancel={() => setDoomed(null)}
        onConfirm={() => doomed && void del(doomed)}
      />
    </div>
  );
}
