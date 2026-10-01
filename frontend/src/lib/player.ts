import { createContext, useContext, useSyncExternalStore } from "react";

/**
 * Tiny external store for the media element so that only the components that
 * care about a given derived value re-render (the transcript only re-renders
 * when the active line index changes, not on every timeupdate).
 */
export class PlayerStore {
  el: HTMLMediaElement | null = null;
  time = 0;
  duration = 0;
  playing = false;
  rate = 1;
  muted = false;
  private ls = new Set<() => void>();
  private raf = 0;
  private pendingSeek: number | null = null;
  private autoplayOnSeek = false;

  subscribe = (l: () => void) => {
    this.ls.add(l);
    return () => this.ls.delete(l);
  };
  private emit() {
    this.ls.forEach((l) => l());
  }

  attach(el: HTMLMediaElement | null, fallbackDuration: number) {
    if (this.el === el) return;
    this.detach();
    this.el = el;
    this.duration = fallbackDuration;
    if (!el) return;
    el.addEventListener("timeupdate", this.onTime);
    el.addEventListener("seeked", this.onTime);
    el.addEventListener("play", this.onPlay);
    el.addEventListener("pause", this.onPause);
    el.addEventListener("ended", this.onPause);
    el.addEventListener("loadedmetadata", this.onMeta);
    el.addEventListener("ratechange", this.onRate);
    el.addEventListener("volumechange", this.onVolume);
    el.playbackRate = this.rate;
    if (el.readyState >= 1) this.onMeta();
  }
  detach() {
    const el = this.el;
    if (!el) return;
    el.removeEventListener("timeupdate", this.onTime);
    el.removeEventListener("seeked", this.onTime);
    el.removeEventListener("play", this.onPlay);
    el.removeEventListener("pause", this.onPause);
    el.removeEventListener("ended", this.onPause);
    el.removeEventListener("loadedmetadata", this.onMeta);
    el.removeEventListener("ratechange", this.onRate);
    el.removeEventListener("volumechange", this.onVolume);
    cancelAnimationFrame(this.raf);
    this.el = null;
  }
  private onMeta = () => {
    const el = this.el!;
    if (Number.isFinite(el.duration) && el.duration > 0) this.duration = Math.max(this.duration, el.duration);
    if (this.pendingSeek != null) {
      el.currentTime = this.pendingSeek;
      this.pendingSeek = null;
      if (this.autoplayOnSeek) void el.play().catch(() => {});
    }
    this.emit();
  };
  private onTime = () => {
    if (!this.el) return;
    this.time = this.el.currentTime;
    this.emit();
  };
  private onRate = () => {
    if (!this.el) return;
    this.rate = this.el.playbackRate;
    this.emit();
  };
  private onVolume = () => {
    if (!this.el) return;
    this.muted = this.el.muted;
    this.emit();
  };
  private onPlay = () => {
    this.playing = true;
    this.emit();
    const loop = () => {
      if (!this.el || this.el.paused) return;
      this.time = this.el.currentTime;
      this.emit();
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  };
  private onPause = () => {
    this.playing = false;
    cancelAnimationFrame(this.raf);
    this.onTime();
  };

  seek(t: number, play = false) {
    t = Math.max(0, this.duration ? Math.min(t, this.duration) : t);
    this.time = t;
    this.emit();
    const el = this.el;
    if (!el) return;
    if (el.readyState < 1) {
      this.pendingSeek = t;
      this.autoplayOnSeek = play;
      return;
    }
    el.currentTime = t;
    if (play && el.paused) void el.play().catch(() => {});
  }
  toggle() {
    const el = this.el;
    if (!el) return;
    if (el.paused) void el.play().catch(() => {});
    else el.pause();
  }
  skip(d: number) {
    this.seek(this.time + d);
  }
  toggleMute() {
    if (this.el) this.el.muted = !this.el.muted;
  }
  setRate(r: number) {
    if (this.el) this.el.playbackRate = r;
    this.rate = r;
    this.emit();
  }
}

export const PlayerContext = createContext<PlayerStore | null>(null);

export function usePlayer(): PlayerStore {
  const p = useContext(PlayerContext);
  if (!p) throw new Error("PlayerContext missing");
  return p;
}

/** Subscribe to a derived value; re-renders only when the selector result changes. */
export function usePlayerValue<T>(select: (p: PlayerStore) => T): T {
  const p = usePlayer();
  return useSyncExternalStore(p.subscribe, () => select(p));
}
