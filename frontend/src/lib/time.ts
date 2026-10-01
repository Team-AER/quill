/** 75.4 -> "1:15", 3725 -> "1:02:05". Negative / NaN -> "0:00". */
export function formatTimestamp(seconds: number | null | undefined, opts: { forceHours?: boolean } = {}): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  if (h > 0 || opts.forceHours) return `${h}:${String(m).padStart(2, "0")}:${ss}`;
  return `${m}:${ss}`;
}

/** Human duration: 3725 -> "1 h 2 min", 95 -> "1 min 35 s", 12 -> "12 s". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "–";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return m > 0 ? `${h} h ${m} min` : `${h} h`;
  if (m > 0) return s > 0 && m < 10 ? `${m} min ${s} s` : `${m} min`;
  return `${s} s`;
}

/** Remaining time from progress fraction and elapsed seconds; null if unknowable. */
export function estimateEta(progress: number | null | undefined, elapsedSeconds: number): number | null {
  if (progress == null || !Number.isFinite(progress) || progress <= 0.01 || progress >= 1) return null;
  if (!Number.isFinite(elapsedSeconds) || elapsedSeconds < 2) return null;
  return (elapsedSeconds * (1 - progress)) / progress;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

const dateFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
const dateYearFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });

/** "Today 09:14", "Yesterday", "12 Sep", "12 Sep 2025". */
export function formatDate(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
  if (days === 0) return `Today ${timeFmt.format(d)}`;
  if (days === 1) return "Yesterday";
  if (d.getFullYear() === now.getFullYear()) return dateFmt.format(d);
  return dateYearFmt.format(d);
}

/** Parse "1:02:05" / "12:30" / "95" into seconds; null when invalid. */
export function parseTimestamp(s: string): number | null {
  const parts = s.trim().split(":");
  if (parts.length === 0 || parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
  return parts.reduce((acc, p) => acc * 60 + Number(p), 0);
}
