import type { Meeting, Notes, Speaker } from "../api/types";
import { speakerName } from "./speakers";
import { formatDate, formatDuration, formatTimestamp } from "./time";
import { toast } from "./toast";

export async function copyText(text: string, done = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Older Safari / insecure origins: select a hidden textarea and use the legacy command.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;opacity:0;pointer-events:none";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) {
      toast("Couldn't copy. Your browser blocked clipboard access.", "error");
      return;
    }
  }
  toast(done);
}

export const momentUrl = (meetingId: string, t?: number | null) =>
  `${location.origin}/m/${meetingId}${t != null && t >= 1 ? `?t=${Math.floor(t)}` : ""}`;

/** "“text” — Priya Raman, 23:14" plus a link back to the moment. */
export function quoteText(meetingId: string, text: string, who: string, t: number) {
  return `“${text.trim()}” — ${who}, ${formatTimestamp(t)}\n${momentUrl(meetingId, t)}`;
}

const owner = (o: string | null, speakers: Map<string, Speaker>) => (!o ? "" : /^S\d+$/.test(o) ? speakerName(o, speakers) : o);
const at = (t: number | null) => (t != null ? ` (${formatTimestamp(t)})` : "");

/** The notes as Markdown for pasting into a doc or chat. */
export function notesMarkdown(m: Meeting, notes: Notes, speakers: Map<string, Speaker>): string {
  const out: string[] = [`# ${m.title}`, `${formatDate(m.created_at)} · ${formatDuration(m.duration_s)}`, ""];
  if (notes.tldr.length) out.push("## TL;DR", ...notes.tldr.map((t) => `- ${t}`), "");
  if (notes.summary) out.push("## Summary", notes.summary.trim(), "");
  if (notes.decisions.length) out.push("## Decisions", ...notes.decisions.map((d) => `- ${d.text}${at(d.t)}`), "");
  if (notes.action_items.length) out.push("## Action items", ...actionLines(notes, speakers), "");
  if (notes.open_questions.length) out.push("## Open questions", ...notes.open_questions.map((d) => `- ${d.text}${at(d.t)}`), "");
  out.push(momentUrl(m.id));
  return out.join("\n");
}

export function actionLines(notes: Notes, speakers: Map<string, Speaker>, done?: Set<string>): string[] {
  return notes.action_items.map((a) => {
    const who = owner(a.owner, speakers);
    return `- [${done?.has(a.task) ? "x" : " "}] ${a.task}${who ? ` — ${who}` : ""}${a.due ? `, due ${a.due}` : ""}${at(a.t)}`;
  });
}
