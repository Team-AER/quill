import { STAGE_LABEL, STAGE_ORDER, type Meeting, type Stage } from "../api/types";
import { estimateEta } from "./time";

export type Tone = "ok" | "run" | "paused" | "failed" | "neutral";

export interface Phase {
  label: string;
  tone: Tone;
  stage: Stage | null;
  overall: number; // 0..1
  reason: string | null;
}

export function sortStages(stages: Stage[] | undefined): Stage[] {
  const s = [...(stages ?? [])];
  s.sort((a, b) => {
    const ia = STAGE_ORDER.indexOf(a.name as never);
    const ib = STAGE_ORDER.indexOf(b.name as never);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  return s;
}

export function stageReason(s: Stage | null | undefined): string | null {
  if (!s) return null;
  if (s.status === "failed") return s.error || s.detail || "Stage failed";
  if (s.status === "paused") return s.detail || s.error || "Paused";
  return null;
}

export function meetingPhase(m: Pick<Meeting, "status" | "stages" | "error" | "current_stage" | "progress">): Phase {
  const status0 = String(m.status);
  if (!m.stages || m.stages.length === 0) {
    // List endpoint before the first SSE snapshot: status + current_stage + progress only.
    const overall = m.progress ?? 0;
    const cur = m.current_stage ?? null;
    const stage: Stage | null = cur ? { name: cur, status: status0 === "running" ? "running" : status0, progress: null } : null;
    if (status0 === "done") return { label: "Ready", tone: "ok", stage: null, overall: 1, reason: null };
    if (status0 === "failed") return { label: "Failed", tone: "failed", stage, overall, reason: m.error ?? null };
    if (status0 === "paused") return { label: "Paused", tone: "paused", stage, overall, reason: null };
    if (status0 === "running" || status0 === "processing")
      return { label: cur ? (STAGE_LABEL[cur] ?? cur) : "Processing", tone: "run", stage, overall, reason: null };
    return { label: status0 === "deleting" ? "Deleting" : "Queued", tone: "neutral", stage: null, overall, reason: null };
  }
  const stages = sortStages(m.stages).filter((s) => s.status !== "skipped");
  const total = stages.length || 1;
  const done = stages.filter((s) => s.status === "done").length;
  const failed = stages.find((s) => s.status === "failed");
  const paused = stages.find((s) => s.status === "paused");
  const running = stages.find((s) => s.status === "running");
  const cur = failed ?? paused ?? running ?? null;
  const overall = Math.min(1, (done + (cur && cur.status !== "failed" ? (cur.progress ?? 0) : 0)) / total);
  const status = String(m.status);
  if (failed || status === "failed")
    return { label: "Failed", tone: "failed", stage: failed ?? null, overall, reason: stageReason(failed) ?? m.error ?? null };
  if (paused || status === "paused") return { label: "Paused", tone: "paused", stage: paused ?? null, overall, reason: stageReason(paused) };
  if (status === "done" || (stages.length > 0 && done === stages.length)) return { label: "Ready", tone: "ok", stage: null, overall: 1, reason: null };
  if (running) return { label: STAGE_LABEL[running.name] ?? running.name, tone: "run", stage: running, overall, reason: null };
  return { label: "Queued", tone: "neutral", stage: null, overall, reason: null };
}

export function stageEta(s: Stage, now = Date.now()): number | null {
  if (s.status !== "running" || !s.started_at) return null;
  const started = Date.parse(s.started_at);
  if (Number.isNaN(started)) return null;
  return estimateEta(s.progress, (now - started) / 1000);
}
