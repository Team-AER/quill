import type { TranscriptLine } from "../api/types";

/**
 * Index of the line that is "current" at time t: the last line whose start <= t.
 * Lines are sorted by start. Returns -1 before the first line.
 * If t falls in a gap after a line ended, the previous line stays current (keeps
 * the highlight stable instead of flickering off between turns).
 */
export function findActiveLine(lines: Pick<TranscriptLine, "start" | "end">[], t: number): number {
  let lo = 0;
  let hi = lines.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (lines[mid].start <= t + 1e-6) {
      ans = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return ans;
}

export interface Band {
  speaker: string;
  start: number;
  end: number;
}

/** Merge consecutive same-speaker lines with gaps < maxGap into timeline bands. */
export function speakerBands(lines: Pick<TranscriptLine, "speaker" | "start" | "end">[], maxGap = 2): Band[] {
  const out: Band[] = [];
  for (const l of lines) {
    const last = out[out.length - 1];
    if (last && last.speaker === l.speaker && l.start - last.end <= maxGap) {
      last.end = Math.max(last.end, l.end);
    } else {
      out.push({ speaker: l.speaker, start: l.start, end: l.end });
    }
  }
  return out;
}

/** Per-speaker talk time in seconds. */
export function talkTime(lines: Pick<TranscriptLine, "speaker" | "start" | "end">[]): Map<string, number> {
  const m = new Map<string, number>();
  for (const l of lines) m.set(l.speaker, (m.get(l.speaker) ?? 0) + Math.max(0, l.end - l.start));
  return m;
}

/** Case-insensitive search; returns matching line indices. */
export function searchLines(lines: Pick<TranscriptLine, "text">[], query: string): number[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const out: number[] = [];
  lines.forEach((l, i) => {
    if (l.text.toLowerCase().includes(q)) out.push(i);
  });
  return out;
}

/** Split text into [plain, match, plain, ...] parts for highlighting. */
export function highlightParts(text: string, query: string): { text: string; hit: boolean }[] {
  const q = query.trim();
  if (!q) return [{ text, hit: false }];
  const lower = text.toLowerCase();
  const ql = q.toLowerCase();
  const parts: { text: string; hit: boolean }[] = [];
  let i = 0;
  while (i < text.length) {
    const j = lower.indexOf(ql, i);
    if (j < 0) {
      parts.push({ text: text.slice(i), hit: false });
      break;
    }
    if (j > i) parts.push({ text: text.slice(i, j), hit: false });
    parts.push({ text: text.slice(j, j + q.length), hit: true });
    i = j + q.length;
  }
  return parts;
}
