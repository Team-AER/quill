import { HardDrive } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { ModelStatus, Settings, SystemInfo } from "../../api/types";
import { formatBytes } from "../../lib/time";
import { errorText, toast } from "../../lib/toast";

function StatusBadge({ status }: { status: ModelStatus }) {
  const cls = status === "ready" ? "ok" : status === "degraded" ? "warn" : status === "disabled" || status === "quarantined" || status === "missing" ? "failed" : "";
  return (
    <span className={`pill ${cls}`}>
      <span className="dot" />
      {status}
    </span>
  );
}

type RetentionMode = "forever" | "after" | "days";

/** Admin: models, transcription model and source retention. */
export function SystemSection({ reloadKey }: { reloadKey: number }) {
  const [sys, setSys] = useState<SystemInfo | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState<RetentionMode>("days");
  const [days, setDays] = useState("14");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, st] = await Promise.all([api.system(), api.settings()]);
      setSys(s);
      setSettings(st);
      const d = st.video_retention_days;
      setMode(d < 0 ? "forever" : d === 0 ? "after" : "days");
      if (d > 0) setDays(String(d));
      setErr(null);
    } catch (e) {
      setErr(errorText(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load, reloadKey]);

  const chooseStt = async (model: string) => {
    if (!settings || model === settings.stt_model) return;
    const prev = settings;
    setSettings({ ...settings, stt_model: model });
    try {
      setSettings(await api.updateSettings({ stt_model: model }));
      toast(`Transcription now uses ${model}`);
      setSys(await api.system());
    } catch (e) {
      setSettings(prev);
      toast(errorText(e), "error");
    }
  };

  const retentionValue = mode === "forever" ? -1 : mode === "after" ? 0 : Math.max(1, Math.min(3650, Number(days) || 14));
  const saveRetention = async () => {
    setSaving(true);
    try {
      setSettings(await api.updateSettings({ video_retention_days: retentionValue }));
      toast("Retention saved");
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setSaving(false);
    }
  };

  const diskUsed = sys?.disk_total_bytes && sys.disk_free_bytes != null ? 1 - sys.disk_free_bytes / sys.disk_total_bytes : null;

  return (
    <>
        {err && (
          <div className="banner failed" role="alert" style={{ marginBottom: 16 }}>
            <span className="b-text">{err}</span>
          </div>
        )}

        <section className="cardsheet" aria-labelledby="models-h">
          <h2 id="models-h">Models</h2>
          <p className="sub">Live status from the llm-proxy catalog. A disabled model pauses its stage; Quill resumes when it's back.</p>
          {sys ? (
            (["stt", "vision", "text"] as const).map((k) => {
              const m = sys[k];
              return (
                <div key={k} className="model-row">
                  <span className="role">{k === "stt" ? "Transcription" : k === "vision" ? "Vision" : "Notes"}</span>
                  <code>{m?.model ?? "–"}</code>
                  <StatusBadge status={m?.status ?? "unknown"} />
                </div>
              );
            })
          ) : (
            <div className="skeleton" style={{ height: 96 }} />
          )}
          {sys && (
            <div className="model-row">
              <span className="role">Storage</span>
              <div>
                <div className="num" style={{ fontSize: 12, marginBottom: 4, display: "flex", gap: 6, alignItems: "center" }}>
                  <HardDrive size={12} />
                  {formatBytes(sys.disk_free_bytes)} free{sys.disk_total_bytes ? ` of ${formatBytes(sys.disk_total_bytes)}` : ""}
                </div>
                {diskUsed != null && (
                  <div className={`progress ${diskUsed > 0.85 ? "failed" : diskUsed > 0.7 ? "paused" : ""}`} style={{ maxWidth: 320 }}>
                    <i style={{ width: `${diskUsed * 100}%` }} />
                  </div>
                )}
              </div>
              <span className="muted" style={{ fontSize: 12 }}>
                {sys.worker?.queue_length != null ? `${sys.worker.queue_length} in queue` : ""}
              </span>
            </div>
          )}
        </section>

        <section className="cardsheet" aria-labelledby="stt-h">
          <h2 id="stt-h">Transcription model</h2>
          <p className="sub">Only models that are ready or degraded in llm-proxy can be chosen. Changing it affects new transcription work.</p>
          <div role="radiogroup" aria-labelledby="stt-h">
            {settings?.stt_model_options.map((o) => (
              <label key={o.model} className={`radio-row ${o.selectable ? "" : "disabled"}`} title={o.selectable ? undefined : `${o.model} is ${o.status} in llm-proxy`}>
                <input type="radio" name="stt" checked={settings.stt_model === o.model} disabled={!o.selectable} onChange={() => void chooseStt(o.model)} />
                <code>{o.model}</code>
                <span className="spacer" />
                <StatusBadge status={o.status} />
              </label>
            ))}
            {settings && settings.stt_model_options.length === 0 && <p className="muted">The server did not report any choices.</p>}
          </div>
        </section>

        <section className="cardsheet" aria-labelledby="ret-h">
          <h2 id="ret-h">Source video retention</h2>
          <p className="sub">Audio, transcripts, frames and notes are kept until a meeting is deleted. This only controls the original upload.</p>
          <div className="inline-form">
            <label className="field" style={{ flex: "0 1 240px" }}>
              <span>Keep source video</span>
              <select className="select" value={mode} onChange={(e) => setMode(e.target.value as RetentionMode)}>
                <option value="days">For a number of days</option>
                <option value="after">Delete after processing</option>
                <option value="forever">Forever</option>
              </select>
            </label>
            {mode === "days" && (
              <label className="field" style={{ flex: "0 1 120px" }}>
                <span>Days</span>
                <input className="input" type="number" min={1} max={3650} value={days} onChange={(e) => setDays(e.target.value)} />
              </label>
            )}
            <button type="button" className="btn primary" onClick={() => void saveRetention()} disabled={saving || !settings || retentionValue === settings.video_retention_days}>
              Save
            </button>
          </div>
        </section>

    </>
  );
}
