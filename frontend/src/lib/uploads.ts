import { UNREADABLE, probeReadable } from "./readable";
import * as tus from "tus-js-client";
import { useSyncExternalStore } from "react";

export const CHUNK_SIZE = 64 * 1024 * 1024;
export const MAX_BYTES = 10 * 1024 ** 3;

export interface UploadOptions {
  title: string;
  language: string;
  expectedSpeakers: string; // "" = auto
  audioOnly: boolean;
}

export type UploadStatus = "starting" | "uploading" | "paused" | "done" | "error";

export interface UploadItem {
  key: string;
  name: string;
  size: number;
  sent: number;
  speed: number; // bytes/s, smoothed
  eta: number | null;
  status: UploadStatus;
  resumed: boolean;
  error?: string;
  meetingId?: string;
  upload: tus.Upload;
}

export interface Interrupted {
  urlStorageKey: string;
  uploadUrl: string | null;
  name: string;
  size: number | null;
  title: string;
  creationTime: string;
}

let items: UploadItem[] = [];
let interrupted: Interrupted[] = [];
const ls = new Set<() => void>();
const emit = () => ls.forEach((l) => l());
const completeListeners = new Set<(meetingId: string | undefined) => void>();

function update(key: string, patch: Partial<UploadItem>) {
  items = items.map((it) => (it.key === key ? { ...it, ...patch } : it));
  emit();
}

export function onUploadComplete(l: (meetingId: string | undefined) => void) {
  completeListeners.add(l);
  return () => {
    completeListeners.delete(l);
  };
}

export async function refreshInterrupted() {
  try {
    const all = await tus.defaultOptions.urlStorage.findAllUploads();
    const active = new Set(items.filter((i) => i.status !== "done").map((i) => i.upload.url));
    interrupted = all
      .filter((p) => !active.has(p.uploadUrl))
      .map((p) => ({
        urlStorageKey: p.urlStorageKey,
        uploadUrl: p.uploadUrl,
        name: p.metadata?.filename ?? "Unknown file",
        title: p.metadata?.title ?? "",
        size: p.size,
        creationTime: p.creationTime,
      }))
      .sort((a, b) => b.creationTime.localeCompare(a.creationTime));
  } catch {
    interrupted = [];
  }
  emit();
}

export async function discardInterrupted(it: Interrupted) {
  try {
    if (it.uploadUrl) await tus.Upload.terminate(it.uploadUrl);
  } catch {
    /* already gone */
  }
  await tus.defaultOptions.urlStorage.removeUpload(it.urlStorageKey);
  await refreshInterrupted();
}

export function startUpload(file: File, opts: UploadOptions): string | null {
  if (file.size > MAX_BYTES) return `${file.name} is larger than the 10 GB limit`;
  const key = `${file.name}:${file.size}:${file.lastModified}:${Math.random().toString(36).slice(2, 7)}`;
  const metadata: Record<string, string> = {
    filename: file.name,
    filetype: file.type || "application/octet-stream",
    title: opts.title || file.name.replace(/\.[^.]+$/, ""),
    language: opts.language,
    audio_only: opts.audioOnly ? "true" : "false",
  };
  if (opts.expectedSpeakers) metadata.expected_speakers = opts.expectedSpeakers;

  let lastT = performance.now();
  let lastBytes = 0;
  let speed = 0;

  const upload: tus.Upload = new tus.Upload(file, {
    endpoint: "/api/uploads",
    chunkSize: CHUNK_SIZE,
    retryDelays: [0, 1000, 3000, 5000, 10000, 20000, 30000],
    metadata,
    storeFingerprintForResuming: true,
    removeFingerprintOnSuccess: true,
    onProgress(sent, total) {
      const now = performance.now();
      const dt = (now - lastT) / 1000;
      if (dt > 0.25) {
        const inst = (sent - lastBytes) / dt;
        speed = speed ? speed * 0.7 + inst * 0.3 : inst;
        lastT = now;
        lastBytes = sent;
      }
      const eta = speed > 0 ? (total - sent) / speed : null;
      update(key, { sent, speed, eta, status: "uploading" });
    },
    onAfterResponse(_req, res) {
      const mid = res.getHeader("Quill-Meeting-Id");
      if (mid) update(key, { meetingId: mid });
    },
    onSuccess() {
      const it = items.find((i) => i.key === key);
      update(key, { status: "done", sent: file.size, eta: 0 });
      completeListeners.forEach((l) => l(it?.meetingId));
      // clear finished rows after a moment
      window.setTimeout(() => {
        items = items.filter((i) => i.key !== key);
        emit();
      }, 6000);
    },
    onError(err) {
      const msg = err instanceof Error ? err.message : String(err);
      // tus DetailedError messages are long; keep the useful tail
      const body = /response text: (.*?)(, request id|$)/.exec(msg)?.[1];
      let pretty = msg;
      try {
        if (body) pretty = JSON.parse(body).error ?? body;
      } catch {
        pretty = body ?? msg;
      }
      if (isReadError(msg)) pretty = UNREADABLE;
      update(key, { status: "error", error: pretty });
      // A failure with no server response may be the browser failing to read the file
      // (it then only reports a generic network error), so check the file again.
      const noResponse = !(err as { originalResponse?: unknown }).originalResponse;
      if (noResponse && pretty !== UNREADABLE) {
        void probeReadable(file).then((problem) => {
          if (problem) update(key, { status: "error", error: problem });
        });
      }
    },
  });

  items = [
    ...items,
    { key, name: file.name, size: file.size, sent: 0, speed: 0, eta: null, status: "starting", resumed: false, upload },
  ];
  emit();

  void probeReadable(file)
    .then((problem) => {
      if (problem) {
        update(key, { status: "error", error: problem });
        return;
      }
      return upload.findPreviousUploads().then((prev) => {
        if (prev.length) {
          upload.resumeFromPreviousUpload(prev[0]);
          update(key, { resumed: true });
        }
        upload.start();
        void refreshInterrupted();
      });
    })
    .catch(() => upload.start());
  return null;
}

function isReadError(msg: string) {
  return /NotReadable|NotFoundError|ERR_FILE|body stream|could not be read|FileReader/i.test(msg);
}

export function pauseUpload(key: string) {
  const it = items.find((i) => i.key === key);
  if (!it) return;
  void it.upload.abort(false);
  update(key, { status: "paused", speed: 0, eta: null });
}

export function resumeUpload(key: string) {
  const it = items.find((i) => i.key === key);
  if (!it) return;
  update(key, { status: "uploading", error: undefined });
  it.upload.start();
}

export async function cancelUpload(key: string) {
  const it = items.find((i) => i.key === key);
  if (!it) return;
  items = items.filter((i) => i.key !== key);
  emit();
  try {
    await it.upload.abort(true);
  } catch {
    /* server may already have dropped it */
  }
  void refreshInterrupted();
}

export function hasActiveUploads() {
  return items.some((i) => i.status === "uploading" || i.status === "starting");
}

const snap = () => items;
const snapI = () => interrupted;
const sub = (l: () => void) => {
  ls.add(l);
  return () => ls.delete(l);
};
export function useUploads() {
  return useSyncExternalStore(sub, snap);
}
export function useInterrupted() {
  return useSyncExternalStore(sub, snapI);
}

window.addEventListener("beforeunload", (e) => {
  if (hasActiveUploads()) {
    e.preventDefault();
  }
});
