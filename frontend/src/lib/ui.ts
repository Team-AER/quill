import { useSyncExternalStore } from "react";
import { toast } from "./toast";

/** App-wide overlays: the ⌘K switcher, the shortcuts sheet and the upload sheet. */
interface UI {
  palette: boolean;
  shortcuts: boolean;
  files: File[] | null;
}

let ui: UI = { palette: false, shortcuts: false, files: null };
const ls = new Set<() => void>();
const set = (p: Partial<UI>) => {
  ui = { ...ui, ...p };
  ls.forEach((l) => l());
};

export const openPalette = () => set({ palette: true, shortcuts: false });
export const closePalette = () => set({ palette: false });
export const openShortcuts = () => set({ shortcuts: true, palette: false });
export const closeShortcuts = () => set({ shortcuts: false });

const MEDIA = /^(video|audio)\//;
const MEDIA_EXT = /\.(mp4|mov|mkv|webm|avi|m4v|m4a|mp3|wav|flac|ogg|opus|aac|wma)$/i;

/** Queue files for the upload sheet; non-media files are dropped with a note. */
export function queueFiles(list: FileList | File[]) {
  const all = Array.from(list);
  const files = all.filter((f) => f.size > 0 && (MEDIA.test(f.type) || MEDIA_EXT.test(f.name)));
  const skipped = all.length - files.length;
  if (skipped) toast(`${skipped} file${skipped === 1 ? " isn't" : "s aren't"} audio or video and ${skipped === 1 ? "was" : "were"} skipped`, "error");
  if (files.length) set({ files: [...(ui.files ?? []), ...files], palette: false });
}
export const setQueuedFiles = (files: File[] | null) => set({ files: files && files.length ? files : null });

let picker: (() => void) | null = null;
/** The Shell registers its hidden file input here so any screen can open it. */
export function registerFilePicker(fn: (() => void) | null) {
  picker = fn;
}
export function openFilePicker() {
  picker?.();
}

export function useUI(): UI {
  return useSyncExternalStore(
    (l) => {
      ls.add(l);
      return () => ls.delete(l);
    },
    () => ui,
  );
}

/** True when a key press belongs to a text field or an open dialog. */
export function isTyping(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  if (!t) return false;
  return t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || !!t.closest("dialog[open]");
}
