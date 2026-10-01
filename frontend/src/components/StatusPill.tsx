import { AlertTriangle, Check, Loader2, Pause } from "lucide-react";
import type { Meeting } from "../api/types";
import { meetingPhase } from "../lib/status";

export function StatusPill({ meeting }: { meeting: Pick<Meeting, "status" | "stages" | "error"> }) {
  const p = meetingPhase(meeting);
  const cls = p.tone === "ok" ? "ok" : p.tone === "run" ? "run" : p.tone === "paused" ? "paused" : p.tone === "failed" ? "failed" : "";
  const icon =
    p.tone === "ok" ? <Check size={11} strokeWidth={2.5} /> :
    p.tone === "run" ? <Loader2 size={11} className="spin" strokeWidth={2.5} /> :
    p.tone === "paused" ? <Pause size={11} strokeWidth={2.5} /> :
    p.tone === "failed" ? <AlertTriangle size={11} strokeWidth={2.5} /> : <span className="dot" />;
  return (
    <span className={`pill ${cls}`} title={p.reason ?? undefined}>
      {icon}
      {p.label}
      {p.tone === "run" && p.stage?.progress != null && <span className="num">{Math.round(p.stage.progress * 100)}%</span>}
    </span>
  );
}
