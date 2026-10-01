import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronDown, ChevronUp, Copy, Crosshair, Link2, Search, Users, X } from "lucide-react";
import { forwardRef, memo, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import type { Speaker, TranscriptLine } from "../../api/types";
import { Avatar } from "../../components/Avatars";
import { copyText, momentUrl, quoteText } from "../../lib/copy";
import { usePlayer, usePlayerValue } from "../../lib/player";
import { speakerName, speakerSlot, speakerVar } from "../../lib/speakers";
import { formatTimestamp } from "../../lib/time";
import { findActiveLine, highlightParts, searchLines } from "../../lib/transcript";

interface Props {
  meetingId: string;
  lines: TranscriptLine[];
  speakers: Map<string, Speaker>;
  emptyHint: string;
  /** Show while transcription is still producing lines. */
  growing?: boolean;
}

export interface TranscriptHandle {
  focusSearch: () => void;
  toggleFollow: () => void;
  soloSpeaker: (label: string) => void;
}

export const Transcript = forwardRef<TranscriptHandle, Props>(function Transcript({ meetingId, lines, speakers, emptyHint, growing }, ref) {
  const store = usePlayer();
  const scroller = useRef<HTMLDivElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const [follow, setFollow] = useState(true);
  const [query, setQuery] = useState("");
  const [hitIdx, setHitIdx] = useState(0);
  const [hidden, setHidden] = useState<Set<string>>(new Set());

  const labels = useMemo(() => {
    const s = new Set<string>();
    speakers.forEach((_, k) => s.add(k));
    lines.forEach((l) => s.add(l.speaker));
    return [...s].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  }, [lines, speakers]);

  // Speaker filter: the list shows a subset; `shown[i]` is an index into `lines`.
  const shown = useMemo(() => (hidden.size ? lines.map((_, i) => i).filter((i) => !hidden.has(lines[i].speaker)) : null), [lines, hidden]);
  const view = useMemo(() => (shown ? shown.map((i) => lines[i]) : lines), [shown, lines]);
  const activeFull = usePlayerValue((p) => findActiveLine(lines, p.time));
  const active = useMemo(() => {
    if (!shown) return activeFull;
    // Nearest visible line at or before the current one.
    let lo = 0;
    let hi = shown.length - 1;
    let ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (shown[mid] <= activeFull) {
        ans = mid;
        lo = mid + 1;
      } else hi = mid - 1;
    }
    return ans >= 0 && shown[ans] === activeFull ? ans : -1;
  }, [shown, activeFull]);
  const playing = usePlayerValue((p) => p.playing);

  const hits = useMemo(() => searchLines(view, query), [view, query]);
  const hitSet = useMemo(() => new Set(hits), [hits]);
  const curHit = hits.length ? hits[Math.min(hitIdx, hits.length - 1)] : -1;

  const virt = useVirtualizer({
    count: view.length,
    getScrollElement: () => scroller.current,
    estimateSize: (i) => (i === 0 || view[i - 1]?.speaker !== view[i]?.speaker ? 74 : 42),
    getItemKey: (i) => view[i]?.id ?? i,
    overscan: 10,
  });

  useImperativeHandle(ref, () => ({
    focusSearch: () => searchInput.current?.focus(),
    toggleFollow: () => setFollow((f) => !f),
    soloSpeaker: (label: string) => setHidden(new Set(labels.filter((l) => l !== label))),
  }));

  // Follow playback.
  useEffect(() => {
    if (follow && active >= 0) virt.scrollToIndex(active, { align: "center", behavior: "auto" });
  }, [follow, active, virt]);

  // Jump to the current search hit.
  useEffect(() => {
    if (curHit >= 0) {
      setFollow(false);
      virt.scrollToIndex(curHit, { align: "center" });
    }
  }, [curHit, virt]);

  const stopFollow = useCallback(() => setFollow(false), []);
  const seekTo = useCallback((t: number) => store.seek(t, true), [store]);

  const step = (d: number) => {
    if (!hits.length) return;
    setHitIdx((i) => (i + d + hits.length) % hits.length);
  };
  const toggleSpeaker = (lab: string, solo: boolean) => {
    setHidden((h) => {
      if (solo) return h.size === labels.length - 1 && !h.has(lab) ? new Set() : new Set(labels.filter((l) => l !== lab));
      const n = new Set(h);
      if (n.has(lab)) n.delete(lab);
      else if (n.size < labels.length - 1) n.add(lab);
      return n;
    });
  };

  return (
    <>
      <div className="tx-head">
        <div className="row">
          <h2>Transcript</h2>
          <span className="count num">
            {lines.length ? (shown ? `${view.length.toLocaleString()} of ${lines.length.toLocaleString()}` : `${lines.length.toLocaleString()} lines`) : ""}
          </span>
          {growing && lines.length > 0 && (
            <span className="live-tag" title="Transcription is still running; new lines appear as they finish">
              <i /> Live
            </span>
          )}
          <span className="spacer" />
          <button
            type="button"
            className={`follow ${follow ? "on" : ""}`}
            aria-pressed={follow}
            onClick={() => setFollow((f) => !f)}
            title={follow ? "Following playback (F)" : "Follow playback (F)"}
          >
            <Crosshair size={13} />
            {follow ? "Following" : "Follow"}
          </button>
        </div>
        <div className="tx-search">
          <div className="search-field">
            <Search size={14} />
            <input
              ref={searchInput}
              className="input"
              placeholder="Search transcript"
              aria-label="Search transcript (/)"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setHitIdx(0);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  step(e.shiftKey ? -1 : 1);
                } else if (e.key === "Escape") {
                  setQuery("");
                  e.currentTarget.blur();
                }
              }}
            />
            {!query && <kbd className="field-kbd">/</kbd>}
          </div>
          {query && (
            <>
              <span className="hits num" aria-live="polite">
                {hits.length ? `${Math.min(hitIdx, hits.length - 1) + 1} of ${hits.length}` : "No matches"}
              </span>
              <button type="button" className="icon-btn sm" onClick={() => step(-1)} disabled={!hits.length} aria-label="Previous match" title="Previous (Shift+Enter)">
                <ChevronUp size={14} />
              </button>
              <button type="button" className="icon-btn sm" onClick={() => step(1)} disabled={!hits.length} aria-label="Next match" title="Next (Enter)">
                <ChevronDown size={14} />
              </button>
              <button type="button" className="icon-btn sm" onClick={() => setQuery("")} aria-label="Clear search" title="Clear">
                <X size={14} />
              </button>
            </>
          )}
        </div>
        {labels.length > 1 && (
          <div className="spk-chips" role="group" aria-label="Show speakers (Alt-click to show only one)">
            {labels.map((lab) => {
              const on = !hidden.has(lab);
              return (
                <button
                  key={lab}
                  type="button"
                  className={`spk-chip ${on ? "on" : ""}`}
                  aria-pressed={on}
                  style={{ "--c": speakerVar(speakerSlot(lab, speakers)) } as React.CSSProperties}
                  onClick={(e) => toggleSpeaker(lab, e.altKey || e.metaKey)}
                  onDoubleClick={() => toggleSpeaker(lab, true)}
                  title={`${on ? "Hide" : "Show"} ${speakerName(lab, speakers)} · double-click to show only them`}
                >
                  <Avatar label={lab} speakers={speakers} size={18} dim={!on} />
                  <span>{speakerName(lab, speakers).split(" ")[0] === "Speaker" ? speakerName(lab, speakers) : speakerName(lab, speakers).split(" ")[0]}</span>
                </button>
              );
            })}
            {hidden.size > 0 && (
              <button type="button" className="spk-chip reset" onClick={() => setHidden(new Set())}>
                Show all
              </button>
            )}
          </div>
        )}
      </div>
      <div
        ref={scroller}
        className="tx-scroll"
        onWheel={stopFollow}
        onTouchMove={stopFollow}
        onKeyDown={(e) => {
          if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(e.key)) stopFollow();
        }}
        tabIndex={-1}
      >
        {lines.length === 0 ? (
          <div className="tx-empty">
            {growing ? (
              <div className="tx-skel" aria-hidden>
                {[80, 62, 90, 45, 70].map((w, i) => (
                  <span key={i} className="skeleton" style={{ width: `${w}%` }} />
                ))}
              </div>
            ) : (
              <Users size={24} style={{ marginBottom: 6, opacity: 0.6 }} />
            )}
            <div>{emptyHint}</div>
          </div>
        ) : (
          <div className="tx-inner" style={{ height: virt.getTotalSize() }}>
            {virt.getVirtualItems().map((vi) => {
              const l = view[vi.index];
              const first = vi.index === 0 || view[vi.index - 1].speaker !== l.speaker;
              return (
                <div
                  key={vi.key}
                  data-index={vi.index}
                  ref={virt.measureElement}
                  className={`tx-line${first ? " speaker-start" : ""}${vi.index === active ? " active" : ""}${hitSet.has(vi.index) ? " hit" : ""}`}
                  style={{ transform: `translateY(${vi.start}px)` }}
                  aria-current={vi.index === active ? "true" : undefined}
                >
                  <Line
                    meetingId={meetingId}
                    line={l}
                    first={first}
                    speakers={speakers}
                    query={query}
                    isCurHit={vi.index === curHit}
                    isActive={vi.index === active}
                    playing={vi.index === active && playing}
                    onSeek={seekTo}
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>
      {!follow && lines.length > 0 && playing && active >= 0 && (
        <button type="button" className="btn primary follow-btn" onClick={() => setFollow(true)}>
          <Crosshair size={14} /> Back to current line
        </button>
      )}
    </>
  );
});

const Line = memo(function Line({
  meetingId,
  line,
  first,
  speakers,
  query,
  isCurHit,
  isActive,
  playing,
  onSeek,
}: {
  meetingId: string;
  line: TranscriptLine;
  first: boolean;
  speakers: Map<string, Speaker>;
  query: string;
  isCurHit: boolean;
  isActive: boolean;
  playing: boolean;
  onSeek: (t: number) => void;
}) {
  const slot = speakerSlot(line.speaker, speakers);
  const name = speakerName(line.speaker, speakers);
  const parts = highlightParts(line.text, query);
  return (
    <div
      className="l"
      onClick={(e) => {
        if (window.getSelection()?.toString()) return; // let people copy quotes
        if ((e.target as HTMLElement).closest(".l-actions")) return;
        onSeek(line.start);
      }}
    >
      <div className="l-gutter">
        {first ? (
          <Avatar label={line.speaker} speakers={speakers} size={28} />
        ) : (
          <button type="button" className="t" onClick={(e) => (e.stopPropagation(), onSeek(line.start))} aria-label={`Play from ${formatTimestamp(line.start)}, ${name}`}>
            {formatTimestamp(line.start)}
          </button>
        )}
      </div>
      <div className="l-body">
        {first && (
          <div className="who">
            <span style={{ color: speakerVar(slot) }}>{name}</span>
            <button type="button" className="t" onClick={(e) => (e.stopPropagation(), onSeek(line.start))} aria-label={`Play from ${formatTimestamp(line.start)}, ${name}`}>
              {formatTimestamp(line.start)}
            </button>
          </div>
        )}
        <div className="txt">
          {isActive && (
            <span className={`eq ${playing ? "on" : ""}`} aria-hidden>
              <i />
              <i />
              <i />
            </span>
          )}
          {!first && <span className="sr-only">{name}: </span>}
          {parts.map((p, i) =>
            p.hit ? (
              <mark key={i} className={isCurHit ? "cur" : undefined}>
                {p.text}
              </mark>
            ) : (
              <span key={i}>{p.text}</span>
            ),
          )}
          {(Boolean(line.overlap) || (line.interjections && line.interjections.length > 0)) && (
            <span className="badges">
              {Boolean(line.overlap) && (
                <span className="badge-overlap" title="Two or more people spoke at the same time; attribution may be less reliable">
                  Overlap
                </span>
              )}
              {line.interjections?.map((ij, i) => (
                <span key={i} className="badge-inter" title={`${speakerName(ij.speaker, speakers)} interjected at ${formatTimestamp(ij.start)}`}>
                  <i style={{ background: speakerVar(speakerSlot(ij.speaker, speakers)) }} />
                  {speakers.get(ij.speaker)?.display_name?.split(" ")[0] ?? speakerName(ij.speaker, speakers)}
                </span>
              ))}
            </span>
          )}
        </div>
      </div>
      <div className="l-actions">
        <button type="button" className="icon-btn sm" aria-label="Copy quote" title="Copy quote with a link" onClick={() => void copyText(quoteText(meetingId, line.text, name, line.start), "Quote copied")}>
          <Copy size={13} />
        </button>
        <button type="button" className="icon-btn sm" aria-label="Copy link to this moment" title="Copy link to this moment" onClick={() => void copyText(momentUrl(meetingId, line.start), `Link to ${formatTimestamp(line.start)} copied`)}>
          <Link2 size={13} />
        </button>
      </div>
    </div>
  );
});
