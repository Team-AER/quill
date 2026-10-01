import { useMemo, useRef, useState } from "react";
import { frameImageUrl } from "../../api/client";
import type { Chapter, Frame, Moment, Speaker, TranscriptLine } from "../../api/types";
import { usePlayer, usePlayerValue } from "../../lib/player";
import { speakerName, speakerSlot, speakerVar } from "../../lib/speakers";
import { formatTimestamp } from "../../lib/time";
import { findActiveLine, speakerBands } from "../../lib/transcript";

interface Props {
  duration: number;
  lines: TranscriptLine[];
  speakers: Map<string, Speaker>;
  chapters: Chapter[];
  frames: Frame[];
  moments: Moment[];
  onFrame: (f: Frame) => void;
  onSpeaker?: (label: string) => void;
}

interface Marker {
  t: number;
  frame?: Frame;
  label: string;
  key: string;
}

/** The meeting at a glance: chapters, who spoke when, and key moments on one time axis. */
export function Timeline({ duration, lines, speakers, chapters, frames, moments, onFrame, onSpeaker }: Props) {
  const store = usePlayer();
  const track = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const dur = usePlayerValue((p) => p.duration) || duration || 1;
  const nowS = usePlayerValue((p) => Math.floor(p.time));
  const curCh = usePlayerValue((p) => {
    let idx = -1;
    chapters.forEach((c, i) => {
      if (c.start <= p.time) idx = i;
    });
    return idx;
  });

  const labels = useMemo(() => {
    const s = new Set<string>();
    speakers.forEach((_, k) => s.add(k));
    lines.forEach((l) => s.add(l.speaker));
    return [...s].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  }, [lines, speakers]);
  const bands = useMemo(() => speakerBands(lines, 1.5), [lines]);
  const bandsBy = useMemo(() => {
    const m = new Map<string, typeof bands>();
    bands.forEach((b) => m.set(b.speaker, [...(m.get(b.speaker) ?? []), b]));
    return m;
  }, [bands]);

  const markers = useMemo(() => {
    const byMoment = new Map(frames.filter((f) => f.moment_id != null).map((f) => [f.moment_id!, f]));
    const out: Marker[] = frames
      .filter((f) => (f.relevance ?? 1) > 0)
      .map((f) => ({ t: f.t, frame: f, label: f.title || f.caption || "Key moment", key: `f${f.id}` }));
    for (const m of moments) {
      if (byMoment.has(m.id)) continue;
      if (frames.some((f) => Math.abs(f.t - m.t) < 1)) continue;
      out.push({ t: m.t, label: m.why || "Key moment", key: `m${m.id}` });
    }
    return out.sort((a, b) => a.t - b.t);
  }, [frames, moments]);

  const tAt = (clientX: number) => {
    const r = track.current!.getBoundingClientRect();
    return Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * dur;
  };

  // What the hover preview shows: the latest frame at or before t, the chapter, who is talking.
  const preview = useMemo(() => {
    if (hover == null) return null;
    let frame: Frame | null = null;
    for (const f of frames) if (f.t <= hover + 1 && (f.relevance ?? 1) > 0) frame = f;
    if (frame && hover - frame.t > 600) frame = null;
    let ch: Chapter | null = null;
    for (const c of chapters) if (c.start <= hover) ch = c;
    const li = findActiveLine(lines, hover);
    const line = li >= 0 && lines[li].end >= hover - 2 ? lines[li] : null;
    return { frame, ch, who: line ? line.speaker : null };
  }, [hover, frames, chapters, lines]);

  const many = labels.length > 5;
  const pct = (t: number) => `${(t / dur) * 100}%`;
  const tipLeft = hover != null ? `clamp(96px, ${pct(hover)}, calc(100% - 96px))` : undefined;

  return (
    <section className={`map ${many ? "dense" : ""}`} aria-label="Meeting map">
      <div className="map-labels" aria-hidden>
        {chapters.length > 0 && <span className="ml-row ml-head">Chapters</span>}
        {labels.map((lab) => (
          <button key={lab} type="button" tabIndex={-1} className="ml-row ml-spk" onClick={() => onSpeaker?.(lab)} title={speakerName(lab, speakers)}>
            <i style={{ background: speakerVar(speakerSlot(lab, speakers)) }} />
            <span>{speakerName(lab, speakers)}</span>
          </button>
        ))}
        {markers.length > 0 && <span className="ml-row ml-head ml-moments">Key moments</span>}
      </div>
      <div
        ref={track}
        className="map-track"
        role="slider"
        tabIndex={0}
        aria-label="Timeline"
        aria-valuemin={0}
        aria-valuemax={Math.round(dur)}
        aria-valuenow={nowS}
        aria-valuetext={formatTimestamp(nowS)}
        onPointerDown={(e) => {
          if ((e.target as HTMLElement).closest("button")) return;
          e.currentTarget.setPointerCapture(e.pointerId);
          store.seek(tAt(e.clientX));
        }}
        onPointerMove={(e) => {
          setHover(tAt(e.clientX));
          if (e.buttons === 1 && e.currentTarget.hasPointerCapture(e.pointerId)) store.seek(tAt(e.clientX));
        }}
        onPointerLeave={() => setHover(null)}
        onKeyDown={(e) => {
          const step = { ArrowLeft: -5, ArrowRight: 5, PageDown: -60, PageUp: 60 }[e.key];
          if (step) {
            e.preventDefault();
            e.stopPropagation();
            store.skip(step);
          } else if (e.key === "Home") {
            e.preventDefault();
            store.seek(0);
          } else if (e.key === "End") {
            e.preventDefault();
            store.seek(dur);
          }
        }}
      >
        {chapters.length > 0 && (
          <div className="mt-row mt-chapters">
            {chapters.map((c, i) => {
              const end = chapters[i + 1]?.start ?? Math.max(c.end, dur);
              return (
                <button
                  key={i}
                  type="button"
                  tabIndex={-1}
                  className={`mt-chapter ${i === curCh ? "cur" : ""}`}
                  style={{ left: pct(c.start), width: `calc(${pct(end - c.start)} - 2px)` }}
                  onClick={() => store.seek(c.start, true)}
                  title={`${formatTimestamp(c.start)} · ${c.title}`}
                >
                  <span>{c.title}</span>
                </button>
              );
            })}
          </div>
        )}
        {labels.map((lab) => (
          <div key={lab} className="mt-row mt-lane">
            <i className="mt-rail" />
            {(bandsBy.get(lab) ?? []).map((b, i) => (
              <i key={i} className="mt-band" style={{ left: pct(b.start), width: pct(b.end - b.start), background: speakerVar(speakerSlot(lab, speakers)) }} />
            ))}
          </div>
        ))}
        {markers.length > 0 && (
          <div className="mt-row mt-moments">
            {markers.map((m) => (
              <button
                key={m.key}
                type="button"
                className={`mt-marker ${m.frame ? "frame" : "text"}`}
                style={{ left: pct(m.t) }}
                aria-label={`Key moment at ${formatTimestamp(m.t)}: ${m.label}`}
                onClick={() => (m.frame ? onFrame(m.frame) : store.seek(m.t, true))}
              >
                {m.frame ? <img src={frameImageUrl(m.frame, true)} alt="" loading="lazy" decoding="async" /> : <i />}
              </button>
            ))}
          </div>
        )}
        <Head dur={dur} />
        {hover != null && <i className="mt-hover" style={{ left: pct(hover) }} />}
        {hover != null && preview && (
          <div className="mt-tip" style={{ left: tipLeft }}>
            {preview.frame && <img src={frameImageUrl(preview.frame, true)} alt="" />}
            <div className="mt-tip-body">
              <b className="num">
                {formatTimestamp(hover)}
                {preview.ch ? ` · ${preview.ch.title}` : ""}
              </b>
              {preview.who && (
                <span>
                  <i style={{ background: speakerVar(speakerSlot(preview.who, speakers)) }} />
                  {speakerName(preview.who, speakers)} speaking
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function Head({ dur }: { dur: number }) {
  const pct = usePlayerValue((p) => Math.round((p.time / dur) * 4000) / 40);
  return (
    <>
      <i className="mt-played" style={{ width: `${pct}%` }} />
      <i className="mt-head" style={{ left: `${pct}%` }} />
    </>
  );
}
