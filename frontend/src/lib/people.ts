import type { User } from "../api/types";
import { formatDate } from "./time";

/** "Priya Raman", or the part of the email before the @ when no name is set. */
export function displayName(u: Pick<User, "name" | "email"> | null | undefined): string {
  if (!u) return "";
  return u.name?.trim() || u.email.split("@")[0] || u.email;
}

/** Up to two initials from a name or an email. */
export function personInitials(u: Pick<User, "name" | "email">): string {
  const base = u.name?.trim() || u.email.split("@")[0].replace(/[._-]+/g, " ");
  const parts = base.split(/\s+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] ?? "?").slice(0, 2);
  return letters.toUpperCase();
}

export type DeviceKind = "phone" | "tablet" | "desktop";

/** "Safari on iPhone", "Chrome on Windows", "Firefox on Linux" from a User-Agent string. */
export function describeDevice(ua: string): { label: string; kind: DeviceKind } {
  if (!ua) return { label: "Unknown device", kind: "desktop" };
  const os = /iPhone/.test(ua)
    ? "iPhone"
    : /iPad/.test(ua)
      ? "iPad"
      : /Android/.test(ua)
        ? "Android"
        : /Mac OS X|Macintosh/.test(ua)
          ? "macOS"
          : /Windows/.test(ua)
            ? "Windows"
            : /CrOS/.test(ua)
              ? "ChromeOS"
              : /Linux/.test(ua)
                ? "Linux"
                : "";
  const browser = /Edg\//.test(ua)
    ? "Edge"
    : /OPR\//.test(ua)
      ? "Opera"
      : /Firefox\/|FxiOS/.test(ua)
        ? "Firefox"
        : /Chrome\/|CriOS/.test(ua)
          ? "Chrome"
          : /Safari\//.test(ua)
            ? "Safari"
            : /curl|python|httpx/i.test(ua)
              ? "Script"
              : "Browser";
  const kind: DeviceKind = os === "iPhone" || (os === "Android" && /Mobile/.test(ua)) ? "phone" : os === "iPad" || os === "Android" ? "tablet" : "desktop";
  return { label: os ? `${browser} on ${os}` : browser, kind };
}

/** "Just now", "5 min ago", "3 h ago", "Yesterday", "4 days ago", then a date. */
export function relativeTime(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (now.getTime() - t) / 1000);
  if (s < 90) return "Just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const days = Math.floor(s / 86400);
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  return formatDate(iso, now);
}

/** For use mid-sentence: "active 3 h ago", "signed in yesterday", but dates keep their capital ("Sep 22"). */
export function inline(text: string): string {
  return /^(Just|Yesterday|Today)\b/.test(text) ? text[0].toLowerCase() + text.slice(1) : text;
}

/** "in 6 days", "in 5 h", "in 20 min", or "" once it has passed. */
export function timeLeft(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "";
  const s = (new Date(iso).getTime() - now.getTime()) / 1000;
  if (!Number.isFinite(s) || s <= 0) return "";
  if (s < 3600) return `in ${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400 * 2) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} days`;
}

export const MIN_PASSWORD = 8;

/** A rough strength hint (not a policy; the server only enforces the minimum length). */
export function passwordStrength(pw: string): { score: 0 | 1 | 2 | 3; label: string } {
  if (pw.length < MIN_PASSWORD) return { score: 0, label: pw ? `${MIN_PASSWORD - pw.length} more character${MIN_PASSWORD - pw.length === 1 ? "" : "s"}` : `At least ${MIN_PASSWORD} characters` };
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((r) => r.test(pw)).length;
  const unique = new Set(pw.toLowerCase()).size;
  if (unique <= 3 || /^(password|qwerty|12345|letmein|welcome)/i.test(pw)) return { score: 1, label: "Easy to guess" };
  const points = (pw.length >= 12 ? 1 : 0) + (pw.length >= 16 ? 1 : 0) + (classes >= 3 ? 1 : 0) + (/\s/.test(pw.trim()) ? 1 : 0);
  if (points >= 2) return { score: 3, label: "Strong" };
  if (points === 1 || classes >= 2) return { score: 2, label: "Good" };
  return { score: 1, label: "Could be stronger" };
}

const longDate = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" });
const linkDate = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : longDate.format(d);
};

/** A ready-to-paste note for chat, since Quill does not send email. */
export function inviteMessage(link: { url: string; expires_at: string; name?: string | null }, from: string): string {
  const hi = link.name ? `Hi ${link.name.split(" ")[0]},` : "Hi,";
  return [
    `${hi} ${from} invited you to Quill, where we keep our meeting recordings, transcripts and notes.`,
    "",
    `Create your account here: ${link.url}`,
    "",
    `The link works once and expires on ${linkDate(link.expires_at)}.`,
  ].join("\n");
}

export function resetMessage(link: { url: string; expires_at: string; name?: string | null }): string {
  const hi = link.name ? `Hi ${link.name.split(" ")[0]},` : "Hi,";
  return [
    `${hi} here's a link to choose a new Quill password: ${link.url}`,
    "",
    `It works once and expires on ${linkDate(link.expires_at)}. Choosing a new password signs you out on your other devices.`,
  ].join("\n");
}
