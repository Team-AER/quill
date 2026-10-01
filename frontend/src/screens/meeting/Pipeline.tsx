import { AlertTriangle, Check, Circle, Loader2, Minus, Pause, RotateCcw, Settings } from "lucide-react";
import { useEffect, useState } from "react";
import { STAGE_LABEL, type Meeting } from "../../api/types";
import { linkClick } from "../../lib/router";
import { meetingPhase, sortStages, stageEta } from "../../lib/status";
import { formatDuration } from "../../lib/time";

const sentence = (s: string) => (/[.!?…]$/.test(s.trim()) ? s.trim() : `${s.trim()}.`);

function useNow(ms: number) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

/** `onRetry` is left out for people the meeting is shared with: only its owner can re-run stages. */
export function Pipeline({ meeting, isAdmin, onRetry, busy }: { meeting: Meeting; isAdmin: boolean; onRetry?: (stage: string) => void; busy: boolean }) {
  const now = useNow(1000);
  const phase = meetingPhase(meeting);
  const stages = sortStages(meeting.stages);
  if (phase.tone === "ok" || stages.length === 0) return null;
  const failed = stages.find((s) => s.status === "failed");
  const paused = stages.find((s) => s.status === "paused");

  return (
    <section className="pipeline" aria-label="Processing pipeline">
      {paused && (
        <div className="banner paused" role="status">
          <Pause size={14} />
          <span className="b-text">
            <strong>{STAGE_LABEL[paused.name] ?? paused.name} paused.</strong> {sentence(paused.detail || paused.error || "Waiting to resume")} Quill resumes automatically; nothing is lost.
          </span>
          {isAdmin && /model|llm-proxy|disabled/i.test(paused.detail ?? "") && (
            <a className="btn sm" href="/settings/system" onClick={linkClick}>
              <Settings size={13} /> Models
            </a>
          )}
        </div>
      )}
      {failed && (
        <div className="banner failed" role="alert">
          <AlertTriangle size={14} />
          <span className="b-text">
            <strong>{STAGE_LABEL[failed.name] ?? failed.name} failed.</strong> {sentence(failed.error || failed.detail || meeting.error || "Unknown error")}
          </span>
          {onRetry && (
            <button type="button" className="btn sm primary" onClick={() => onRetry(failed.name)} disabled={busy}>
              <RotateCcw size={13} /> Retry
            </button>
          )}
        </div>
      )}
      <ol className="stages" style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {stages.map((s) => {
          const eta = stageEta(s, now);
          const icon =
            s.status === "done" ? <Check size={13} strokeWidth={2.5} /> :
            s.status === "running" ? <Loader2 size={13} className="spin" /> :
            s.status === "paused" ? <Pause size={13} /> :
            s.status === "failed" ? <AlertTriangle size={13} /> :
            s.status === "skipped" ? <Minus size={13} /> : <Circle size={11} />;
          const sub =
            s.status === "running"
              ? `${Math.round((s.progress ?? 0) * 100)}%${eta != null ? ` · ${formatDuration(eta)} left` : ""}`
              : s.status === "paused"
                ? `Paused at ${Math.round((s.progress ?? 0) * 100)}%`
                : s.status === "failed"
                  ? "Failed"
                  : s.status === "skipped"
                    ? "Skipped (audio)"
                    : s.status === "done"
                      ? "Done"
                      : "Waiting";
          return (
            <li key={s.name} className={`stage ${s.status}`} title={s.detail ?? undefined}>
              <div className="s-top">
                {icon}
                {STAGE_LABEL[s.name] ?? s.name}
              </div>
              {(s.status === "running" || s.status === "paused") && (
                <div className={`progress ${s.status === "paused" ? "paused" : ""}`} role="progressbar" aria-label={`${STAGE_LABEL[s.name] ?? s.name} progress`} aria-valuenow={Math.round((s.progress ?? 0) * 100)} aria-valuemin={0} aria-valuemax={100}>
                  <i style={{ width: `${(s.progress ?? 0) * 100}%` }} />
                </div>
              )}
              <div className="s-sub num">{s.status === "running" && s.detail ? `${sub} · ${s.detail}` : sub}</div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
