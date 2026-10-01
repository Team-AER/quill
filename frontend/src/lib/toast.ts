import { useSyncExternalStore } from "react";

export interface Toast {
  id: number;
  text: string;
  kind: "info" | "error";
  action?: { label: string; run: () => void };
}
let toasts: Toast[] = [];
let seq = 1;
const ls = new Set<() => void>();
const emit = () => ls.forEach((l) => l());

export function toast(text: string, kind: Toast["kind"] = "info", action?: Toast["action"], ms = 4000) {
  const t = { id: seq++, text, kind, action };
  toasts = [...toasts, t];
  emit();
  window.setTimeout(() => dismissToast(t.id), ms);
}
export function dismissToast(id: number) {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}
export function useToasts() {
  return useSyncExternalStore(
    (l) => {
      ls.add(l);
      return () => ls.delete(l);
    },
    () => toasts,
  );
}
export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
