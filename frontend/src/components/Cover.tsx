import { AudioLines, Video } from "lucide-react";
import { useState } from "react";
import type { Meeting } from "../api/types";
import { speakerSlot, speakerVar } from "../lib/speakers";

function hash(s: string) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return h >>> 0;
}

/** Speech-bar art for meetings without a picture: seeded by the id, coloured by who spoke. */
function Bars({ m }: { m: Meeting }) {
  const labels = (m.speakers ?? []).map((s) => s.label);
  const n = 28;
  let h = hash(m.id);
  const rnd = () => ((h = Math.imul(h ^ (h >>> 15), 2246822507) >>> 0), (h % 1000) / 1000);
  const bars = Array.from({ length: n }, (_, i) => {
    const run = Math.floor(i / (3 + (hash(m.id + i) % 4)));
    const label = labels.length ? labels[(run + (hash(m.id) % labels.length)) % labels.length] : null;
    return { h: 14 + rnd() * 70, color: label ? speakerVar(speakerSlot(label, m.speakers)) : "var(--faint)" };
  });
  return (
    <span className="cover-bars" aria-hidden>
      {bars.map((b, i) => (
        <i key={i} style={{ height: `${b.h}%`, background: b.color }} />
      ))}
    </span>
  );
}

export function Cover({ m, children }: { m: Meeting; children?: React.ReactNode }) {
  const [broken, setBroken] = useState(false);
  const audio = m.mode === "audio";
  const img = !audio && m.cover_url && !broken ? m.cover_url : null;
  return (
    <span className={`cover ${audio ? "audio" : "video"} ${img ? "has-img" : ""}`}>
      {img ? (
        <img src={img} alt="" loading="lazy" decoding="async" onError={() => setBroken(true)} />
      ) : audio || (m.speakers?.length ?? 0) > 0 ? (
        <Bars m={m} />
      ) : (
        <span className="cover-blank" aria-hidden>
          <Video size={22} />
        </span>
      )}
      <span className="cover-mode" aria-hidden>
        {audio ? <AudioLines size={12} /> : <Video size={12} />}
      </span>
      {children}
    </span>
  );
}
