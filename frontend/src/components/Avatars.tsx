import type { Speaker } from "../api/types";
import { initials, speakerName, speakerSlot, speakerVar } from "../lib/speakers";

export function Avatar({ label, speakers, size = 28, dim }: { label: string; speakers?: Speaker[] | Map<string, Speaker>; size?: number; dim?: boolean }) {
  const name = speakerName(label, speakers);
  return (
    <span
      className="avatar-spk"
      style={{ width: size, height: size, fontSize: Math.max(8, Math.round(size * 0.38)), background: speakerVar(speakerSlot(label, speakers)), opacity: dim ? 0.45 : undefined }}
      aria-hidden
    >
      {initials(name)}
    </span>
  );
}

/** Overlapping speaker avatars, busiest first; "+N" past `max`. */
export function AvatarStack({ speakers, max = 4, size = 20, title }: { speakers: Speaker[]; max?: number; size?: number; title?: string }) {
  if (!speakers.length) return null;
  const sorted = [...speakers].sort((a, b) => (b.talk_time_s ?? 0) - (a.talk_time_s ?? 0));
  const shown = sorted.slice(0, sorted.length > max ? max - 1 : max);
  const rest = sorted.length - shown.length;
  const names = sorted.map((s) => speakerName(s.label, speakers)).join(", ");
  return (
    <span className="avatar-stack" title={title ?? names} aria-label={`${speakers.length} speaker${speakers.length === 1 ? "" : "s"}: ${names}`}>
      {shown.map((s) => (
        <Avatar key={s.label} label={s.label} speakers={speakers} size={size} />
      ))}
      {rest > 0 && (
        <span className="avatar-spk more" style={{ width: size, height: size, fontSize: Math.max(8, Math.round(size * 0.38)) }} aria-hidden>
          +{rest}
        </span>
      )}
    </span>
  );
}
