import { Info, Maximize, Minimize, Pause, PictureInPicture2, Play, RotateCcw, RotateCw, Volume2, VolumeX, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { mediaUrl } from "../../api/client";
import type { Chapter, Meeting, Speaker, TranscriptLine } from "../../api/types";
import { Menu } from "../../components/Menu";
import { usePlayer, usePlayerValue, type PlayerStore } from "../../lib/player";
import { readPref, writePref } from "../../lib/prefs";
import { speakerSlot } from "../../lib/speakers";
import { formatTimestamp } from "../../lib/time";

export const RATES = [0.75, 1, 1.25, 1.5, 1.75, 2];

interface Props {
  meeting: Meeting;
  lines: TranscriptLine[];
  speakers: Map<string, Speaker>;
  chapters: Chapter[];
  poster?: string | null;
  /** Deep link ?t=… wins over the remembered position. */
  deepLinked: boolean;
}

function chapterAt(chapters: Chapter[], t: number) {
  let idx = -1;
  chapters.forEach((c, i) => {
    if (c.start <= t) idx = i;
  });
  return idx;
}

/** Remember where playback stopped, per meeting, and offer to pick up from there. */
function useResume(store: PlayerStore, id: string, duration: number, deepLinked: boolean) {
  const key = `pos.${id}`;
  const [saved, setSaved] = useState<number | null>(() => {
    const v = readPref<number>(key, 0);
    return !deepLinked && v > 15 && (!duration || v < duration - 15) ? v : null;
  });
  useEffect(() => {
    let last = 0;
    const save = () => {
      const t = store.time;
      if (t < 5) return;
      writePref(key, duration && t > duration - 10 ? null : Math.floor(t));
    };
    const unsub = store.subscribe(() => {
      if (store.time > 2) setSaved(null);
      const now = Date.now();
      if (store.playing && now - last > 5000) {
        last = now;
        save();
      }
    });
    const onHide = () => save();
    window.addEventListener("pagehide", onHide);
    return () => {
      save();
      unsub();
      window.removeEventListener("pagehide", onHide);
    };
  }, [store, key, duration]);
  return { saved, dismiss: () => setSaved(null) };
}

function ResumeChip({ t, onResume, onDismiss }: { t: number; onResume: () => void; onDismiss: () => void }) {
  return (
    <div className="resume">
      <button type="button" className="resume-go" onClick={onResume}>
        <Play size={12} fill="currentColor" /> Resume from {formatTimestamp(t)}
      </button>
      <button type="button" className="resume-x" onClick={onDismiss} aria-label="Dismiss" title="Dismiss">
        <X size={11} />
      </button>
    </div>
  );
}

function Scrubber({ chapters, fallback }: { chapters: Chapter[]; fallback: number }) {
  const store = usePlayer();
  const bar = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const dur = usePlayerValue((p) => p.duration) || fallback || 1;
  const pct = usePlayerValue((p) => Math.round((p.time / dur) * 4000) / 40);
  const now = usePlayerValue((p) => Math.floor(p.time));
  const tAt = (x: number) => {
    const r = bar.current!.getBoundingClientRect();
    return Math.max(0, Math.min(1, (x - r.left) / r.width)) * dur;
  };
  const hc = hover != null ? chapterAt(chapters, hover) : -1;
  return (
    <div
      ref={bar}
      className="scrub"
      role="slider"
      tabIndex={0}
      aria-label="Seek"
      aria-valuemin={0}
      aria-valuemax={Math.round(dur)}
      aria-valuenow={now}
      aria-valuetext={formatTimestamp(now)}
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        store.seek(tAt(e.clientX));
      }}
      onPointerMove={(e) => {
        setHover(tAt(e.clientX));
        if (e.buttons === 1) store.seek(tAt(e.clientX));
      }}
      onPointerLeave={() => setHover(null)}
      onKeyDown={(e) => {
        const step = { ArrowLeft: -5, ArrowRight: 5, PageDown: -60, PageUp: 60 }[e.key];
        if (step) {
          e.preventDefault();
          e.stopPropagation();
          store.skip(step);
        }
      }}
    >
      <div className="scrub-rail">
        <i className="scrub-fill" style={{ width: `${pct}%` }} />
        {chapters.slice(1).map((c, i) => (
          <i key={i} className="scrub-tick" style={{ left: `${(c.start / dur) * 100}%` }} />
        ))}
        <i className="scrub-knob" style={{ left: `${pct}%` }} />
      </div>
      {hover != null && (
        <span className="scrub-tip" style={{ left: `${(hover / dur) * 100}%` }}>
          <b className="num">{formatTimestamp(hover)}</b>
          {hc >= 0 && <span>{chapters[hc].title}</span>}
        </span>
      )}
    </div>
  );
}

function Controls({ chapters, video, wrap }: { chapters: Chapter[]; video: boolean; wrap?: React.RefObject<HTMLDivElement | null> }) {
  const store = usePlayer();
  const playing = usePlayerValue((p) => p.playing);
  const t = usePlayerValue((p) => Math.floor(p.time));
  const dur = usePlayerValue((p) => p.duration);
  const rate = usePlayerValue((p) => p.rate);
  const muted = usePlayerValue((p) => p.muted);
  const ch = usePlayerValue((p) => chapterAt(chapters, p.time));
  const [fs, setFs] = useState(false);
  useEffect(() => {
    const on = () => setFs(document.fullscreenElement != null && document.fullscreenElement === wrap?.current);
    document.addEventListener("fullscreenchange", on);
    return () => document.removeEventListener("fullscreenchange", on);
  }, [wrap]);
  const canPip = video && typeof document !== "undefined" && "pictureInPictureEnabled" in document && document.pictureInPictureEnabled;
  const toggleFs = () => {
    if (document.fullscreenElement) void document.exitFullscreen();
    else if (wrap?.current?.requestFullscreen) void wrap.current.requestFullscreen();
    else (store.el as HTMLVideoElement & { webkitEnterFullscreen?: () => void })?.webkitEnterFullscreen?.();
  };
  return (
    <div className="ctl-row">
      <button type="button" className="ctl-play" onClick={() => store.toggle()} aria-label={playing ? "Pause (K)" : "Play (K)"} title={playing ? "Pause (K)" : "Play (K)"}>
        {playing ? <Pause size={15} fill="currentColor" /> : <Play size={15} fill="currentColor" style={{ marginLeft: 2 }} />}
      </button>
      <button type="button" className="ctl-btn" onClick={() => store.skip(-10)} aria-label="Back 10 seconds (J)" title="Back 10 s (J)">
        <RotateCcw size={16} />
      </button>
      <button type="button" className="ctl-btn" onClick={() => store.skip(10)} aria-label="Forward 10 seconds (L)" title="Forward 10 s (L)">
        <RotateCw size={16} />
      </button>
      <span className="ctl-time num" aria-live="off">
        {formatTimestamp(t, { forceHours: dur >= 3600 })} <span>/ {formatTimestamp(dur)}</span>
      </span>
      {ch >= 0 && <span className="ctl-chapter">{chapters[ch].title}</span>}
      <span className="spacer" />
      <Menu label="Playback speed ([ and ])" buttonClass="ctl-rate num" button={<>{rate}×</>}>
        {(close) =>
          RATES.map((r) => (
            <button
              key={r}
              type="button"
              aria-pressed={r === rate}
              onClick={() => {
                store.setRate(r);
                close();
              }}
            >
              <span className="num">{r}×</span> {r === rate ? "✓" : ""}
            </button>
          ))
        }
      </Menu>
      <button type="button" className="ctl-btn" onClick={() => store.toggleMute()} aria-label={muted ? "Unmute (M)" : "Mute (M)"} title={muted ? "Unmute (M)" : "Mute (M)"}>
        {muted ? <VolumeX size={16} /> : <Volume2 size={16} />}
      </button>
      {canPip && (
        <button
          type="button"
          className="ctl-btn hide-phone"
          onClick={() => {
            const v = store.el as HTMLVideoElement | null;
            if (document.pictureInPictureElement) void document.exitPictureInPicture();
            else void v?.requestPictureInPicture?.().catch(() => {});
          }}
          aria-label="Picture in picture"
          title="Picture in picture"
        >
          <PictureInPicture2 size={16} />
        </button>
      )}
      {video && (
        <button type="button" className="ctl-btn" onClick={toggleFs} aria-label={fs ? "Exit full screen" : "Full screen"} title={fs ? "Exit full screen" : "Full screen"}>
          {fs ? <Minimize size={16} /> : <Maximize size={16} />}
        </button>
      )}
    </div>
  );
}

export function Player({ meeting, lines, speakers, chapters, poster, deepLinked }: Props) {
  const store = usePlayer();
  const wantsVideo = meeting.mode !== "audio" && !meeting.source_deleted_at && meeting.has_video !== 0 && meeting.has_video !== false;
  const [noPicture, setNoPicture] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [idle, setIdle] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const idleTimer = useRef<number | undefined>(undefined);
  const duration = meeting.duration_s ?? 0;
  const playing = usePlayerValue((p) => p.playing);
  const started = usePlayerValue((p) => p.playing || p.time > 0.5);
  const resume = useResume(store, meeting.id, duration, deepLinked);

  const refVideo = useCallback((el: HTMLVideoElement | null) => store.attach(el, duration), [store, duration]);
  const refAudio = useCallback((el: HTMLAudioElement | null) => store.attach(el, duration), [store, duration]);
  useEffect(() => () => store.detach(), [store]);

  const poke = () => {
    setIdle(false);
    window.clearTimeout(idleTimer.current);
    idleTimer.current = window.setTimeout(() => setIdle(true), 2500);
  };
  useEffect(() => () => window.clearTimeout(idleTimer.current), []);

  const onError = () => setErr("The recording couldn't be loaded. The transcript and notes are still available.");
  const chip = resume.saved != null && (
    <ResumeChip
      t={resume.saved}
      onResume={() => {
        store.seek(resume.saved!, true);
        resume.dismiss();
      }}
      onDismiss={resume.dismiss}
    />
  );

  if (wantsVideo && !noPicture) {
    return (
      <div
        ref={wrap}
        className={`player video ${playing && idle ? "idle" : ""} ${started ? "started" : ""}`}
        onMouseMove={poke}
        onMouseLeave={() => playing && setIdle(true)}
        onFocus={poke}
      >
        <video
          ref={refVideo}
          src={mediaUrl(meeting.id)}
          poster={poster ?? undefined}
          playsInline
          preload="metadata"
          onClick={() => store.toggle()}
          onDoubleClick={() => (document.fullscreenElement ? void document.exitFullscreen() : void wrap.current?.requestFullscreen?.())}
          onLoadedMetadata={(e) => {
            // The API falls back to the audio track when the source video is gone.
            if (e.currentTarget.videoWidth === 0) setNoPicture(true);
          }}
          onError={onError}
          aria-label={`Recording of ${meeting.title}`}
        />
        {!started && (
          <button type="button" className="big-play" onClick={() => store.toggle()} aria-label="Play">
            <Play size={26} fill="currentColor" />
          </button>
        )}
        {chip}
        <div className="vc">
          <Scrubber chapters={chapters} fallback={duration} />
          <Controls chapters={chapters} video wrap={wrap} />
        </div>
        {err && <div className="banner failed player-err">{err}</div>}
      </div>
    );
  }

  return (
    <div className="player audio">
      <audio ref={refAudio} src={mediaUrl(meeting.id)} preload="metadata" onError={onError} />
      {chip}
      <Controls chapters={chapters} video={false} />
      <Waveform lines={lines} speakers={speakers} duration={duration} />
      {meeting.mode !== "audio" && (
        <div className="player-note">
          <Info size={13} />
          {meeting.source_deleted_at
            ? "The source video was removed by the retention policy. Playing the extracted audio."
            : "No picture in this recording. Playing audio."}
        </div>
      )}
      {err && <div className="banner failed">{err}</div>}
    </div>
  );
}

/** Speech-activity waveform built from transcript timing (no audio decode needed). */
function Waveform({ lines, speakers, duration }: { lines: TranscriptLine[]; speakers: Map<string, Speaker>; duration: number }) {
  const store = usePlayer();
  const canvas = useRef<HTMLCanvasElement>(null);
  const bars = useRef<{ amp: number; slot: number }[]>([]);
  const [w, setW] = useState(0);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const ro = new ResizeObserver(() => setW(c.clientWidth));
    ro.observe(c);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const n = Math.max(20, Math.floor(w / 4));
    const dur = duration || lines[lines.length - 1]?.end || 1;
    const cover = new Array(n).fill(0);
    const who: Map<string, number>[] = Array.from({ length: n }, () => new Map());
    const bw = dur / n;
    for (const l of lines) {
      const a = Math.max(0, Math.floor(l.start / bw));
      const b = Math.min(n - 1, Math.floor(l.end / bw));
      for (let i = a; i <= b; i++) {
        const s = Math.max(l.start, i * bw);
        const e = Math.min(l.end, (i + 1) * bw);
        if (e > s) {
          cover[i] += (e - s) / bw;
          who[i].set(l.speaker, (who[i].get(l.speaker) ?? 0) + (e - s));
        }
      }
    }
    bars.current = cover.map((c, i) => {
      let best = "";
      let bv = 0;
      who[i].forEach((v, k) => {
        if (v > bv) {
          bv = v;
          best = k;
        }
      });
      const jitter = 0.55 + 0.45 * Math.abs((Math.sin(i * 12.9898) * 43758.5453) % 1);
      return { amp: Math.min(1, c) * jitter, slot: best ? speakerSlot(best, speakers) : -1 };
    });
  }, [w, lines, speakers, duration]);

  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    let raf = 0;
    const draw = () => {
      raf = 0;
      const dpr = window.devicePixelRatio || 1;
      const W = c.clientWidth;
      const H = c.clientHeight;
      if (c.width !== W * dpr) c.width = W * dpr;
      if (c.height !== H * dpr) c.height = H * dpr;
      const ctx = c.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      const cs = getComputedStyle(c);
      const colors = Array.from({ length: 8 }, (_, i) => cs.getPropertyValue(`--spk-${i}`).trim() || "#888");
      const faint = cs.getPropertyValue("--line-strong").trim() || "#ccc";
      const dur = store.duration || duration || 1;
      const played = (store.time / dur) * W;
      const n = bars.current.length;
      const bw = W / n;
      bars.current.forEach((b, i) => {
        const x = i * bw;
        const h = Math.max(3, b.amp * (H - 6));
        ctx.globalAlpha = x < played ? 1 : 0.32;
        ctx.fillStyle = b.slot >= 0 ? colors[b.slot] : faint;
        const y = (H - h) / 2;
        ctx.beginPath();
        ctx.roundRect(x + 0.75, y, Math.max(1, bw - 1.5), h, 1.5);
        ctx.fill();
      });
      ctx.globalAlpha = 1;
    };
    const schedule = () => {
      if (!raf) raf = requestAnimationFrame(draw);
    };
    schedule();
    const unsub = store.subscribe(schedule);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    mq.addEventListener("change", schedule);
    const mo = new MutationObserver(schedule);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      unsub();
      mq.removeEventListener("change", schedule);
      mo.disconnect();
      cancelAnimationFrame(raf);
    };
  }, [store, duration, w, lines, speakers]);

  const tAt = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * (store.duration || duration);
  };
  return (
    <div
      className="wave"
      aria-hidden
      onPointerDown={(e) => store.seek(tAt(e))}
      onPointerMove={(e) => {
        setHover(tAt(e));
        if (e.buttons === 1) store.seek(tAt(e));
      }}
      onPointerLeave={() => setHover(null)}
    >
      <canvas ref={canvas} />
      {hover != null && (
        <span className="scrub-tip" style={{ left: `${(hover / (store.duration || duration || 1)) * 100}%` }}>
          <b className="num">{formatTimestamp(hover)}</b>
        </span>
      )}
    </div>
  );
}
