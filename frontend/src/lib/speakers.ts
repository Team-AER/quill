import type { Speaker } from "../api/types";

export const SPEAKER_HUES = 8;

/** Colour slot 0..7: prefer the stored colour, else derive from "S<n>". */
export function speakerSlot(label: string, speakers?: Speaker[] | Map<string, Speaker>): number {
  const sp = speakers instanceof Map ? speakers.get(label) : speakers?.find((s) => s.label === label);
  if (sp && sp.color != null && Number.isFinite(sp.color)) return ((sp.color % SPEAKER_HUES) + SPEAKER_HUES) % SPEAKER_HUES;
  const n = Number(label.replace(/\D/g, ""));
  return Number.isFinite(n) && n > 0 ? (n - 1) % SPEAKER_HUES : 0;
}

export function speakerName(label: string | null | undefined, speakers?: Speaker[] | Map<string, Speaker>): string {
  if (!label) return "Unassigned";
  const sp = speakers instanceof Map ? speakers.get(label) : speakers?.find((s) => s.label === label);
  if (sp?.display_name) return sp.display_name;
  const n = label.replace(/^S/, "");
  return /^\d+$/.test(n) ? `Speaker ${n}` : label;
}

export function speakerVar(slot: number): string {
  return `var(--spk-${slot})`;
}

export function initials(name: string): string {
  const words = name.replace(/[^\p{L}\p{N} ]/gu, "").split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words[0].toLowerCase() === "speaker" && words[1]) return words[1].slice(0, 2);
  return (words[0][0] + (words[1]?.[0] ?? "")).toUpperCase();
}
