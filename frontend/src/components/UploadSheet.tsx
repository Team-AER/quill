import { AudioLines, Upload, Video, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { navigate, useRoute } from "../lib/router";
import { formatBytes } from "../lib/time";
import { titleFromFilename } from "../lib/title";
import { toast } from "../lib/toast";
import { setQueuedFiles, useUI } from "../lib/ui";
import { MAX_BYTES, startUpload } from "../lib/uploads";

export const LANGUAGES: { id: string; label: string }[] = [
  { id: "auto", label: "Detect automatically" },
  { id: "en", label: "English" },
  { id: "hi", label: "Hindi" },
  { id: "de", label: "German" },
  { id: "fr", label: "French" },
  { id: "es", label: "Spanish" },
  { id: "ja", label: "Japanese" },
  { id: "zh", label: "Chinese" },
];

const isAudio = (f: File) => f.type.startsWith("audio/") || /\.(m4a|mp3|wav|flac|ogg|opus|aac|wma)$/i.test(f.name);

/** Review queued files: editable titles (cleaned from the file name), language, speakers, audio-only. */
export function UploadSheet() {
  const { files } = useUI();
  const route = useRoute();
  const ref = useRef<HTMLDialogElement>(null);
  const [titles, setTitles] = useState<string[]>([]);
  const [language, setLanguage] = useState("auto");
  const [speakers, setSpeakers] = useState("");
  const [audioOnly, setAudioOnly] = useState(false);

  // New files keep the titles already edited for earlier ones.
  useEffect(() => {
    setTitles((prev) => (files ?? []).map((f, i) => prev[i] ?? titleFromFilename(f.name)));
  }, [files]);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (files && !d.open) {
      d.showModal();
      d.querySelector<HTMLInputElement>(".up-main input")?.select();
    }
    if (!files && d.open) d.close();
  }, [files]);

  const list = files ?? [];
  const total = list.reduce((a, f) => a + f.size, 0);
  const tooBig = list.filter((f) => f.size > MAX_BYTES);
  const allAudio = list.length > 0 && list.every(isAudio);

  const remove = (i: number) => {
    setTitles((t) => t.filter((_, k) => k !== i));
    setQueuedFiles(list.filter((_, k) => k !== i));
  };

  const start = () => {
    let started = 0;
    list.forEach((f, i) => {
      const err = startUpload(f, { title: (titles[i] ?? "").trim() || titleFromFilename(f.name), language, expectedSpeakers: speakers, audioOnly });
      if (err) toast(err, "error");
      else started++;
    });
    setQueuedFiles(null);
    if (started && route.name !== "library")
      toast(`Uploading ${started} recording${started === 1 ? "" : "s"}`, "info", { label: "View", run: () => navigate("/") });
  };

  return (
    <dialog ref={ref} className="sheet-dialog upload-sheet" aria-labelledby="up-title" onCancel={(e) => (e.preventDefault(), setQueuedFiles(null))}>
      <form
        method="dialog"
        onSubmit={(e) => {
          e.preventDefault();
          if (!tooBig.length) start();
        }}
      >
        <header className="sd-head">
          <span className="sd-icon" aria-hidden>
            <Upload size={18} />
          </span>
          <div className="sd-title">
            <h2 id="up-title">
              Upload {list.length} recording{list.length === 1 ? "" : "s"}
            </h2>
            <span>{formatBytes(total)} · uploads resume if the connection drops</span>
          </div>
          <button type="button" className="icon-btn" aria-label="Cancel" title="Cancel (Esc)" onClick={() => setQueuedFiles(null)}>
            <X size={16} />
          </button>
        </header>
        <div className="up-files">
          {list.map((f, i) => (
            <div key={`${f.name}:${f.size}:${i}`} className={`up-file ${f.size > MAX_BYTES ? "bad" : ""}`}>
              <span className={`up-kind ${isAudio(f) ? "audio" : ""}`} aria-hidden>
                {isAudio(f) ? <AudioLines size={16} /> : <Video size={16} />}
              </span>
              <div className="up-main">
                <input
                  className="input"
                  aria-label={`Title for ${f.name}`}
                  value={titles[i] ?? ""}
                  placeholder={titleFromFilename(f.name)}
                  onChange={(e) => setTitles((t) => t.map((x, k) => (k === i ? e.target.value : x)))}
                />
                <span className="up-meta" title={f.name}>
                  {f.size > MAX_BYTES ? "Larger than the 10 GB limit" : `${f.name} · ${formatBytes(f.size)}`}
                </span>
              </div>
              <button type="button" className="icon-btn sm" aria-label={`Remove ${f.name}`} title="Remove" onClick={() => remove(i)}>
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
        <div className="up-opts">
          <label className="field">
            <span>Language</span>
            <select className="select" value={language} onChange={(e) => setLanguage(e.target.value)}>
              {LANGUAGES.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Speakers</span>
            <select className="select" value={speakers} onChange={(e) => setSpeakers(e.target.value)}>
              <option value="">Detect automatically</option>
              {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
                <option key={n} value={String(n)}>
                  {n}
                </option>
              ))}
            </select>
          </label>
        </div>
        {!allAudio && (
          <label className="up-check">
            <input type="checkbox" checked={audioOnly} onChange={(e) => setAudioOnly(e.target.checked)} />
            <span>
              <b>Audio only</b>
              <span>Skip key moments and frames. Faster for calls without slides or screen shares.</span>
            </span>
          </label>
        )}
        <footer className="sd-foot">
          <span className="muted">You can keep working while it uploads.</span>
          <button type="button" className="btn" onClick={() => setQueuedFiles(null)}>
            Cancel
          </button>
          <button type="submit" className="btn primary" disabled={!list.length || tooBig.length > 0}>
            {list.length === 1 ? "Start upload" : `Start ${list.length} uploads`}
          </button>
        </footer>
      </form>
    </dialog>
  );
}
