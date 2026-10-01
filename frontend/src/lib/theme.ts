import { useSyncExternalStore } from "react";

export type ThemePref = "system" | "light" | "dark";
const KEY = "quill.theme";

function read(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

// One preference for the whole page, so the rail toggle and Settings stay in step.
let pref: ThemePref = read();
const listeners = new Set<() => void>();

function apply(p: ThemePref) {
  const root = document.documentElement;
  if (p === "system") delete root.dataset.theme;
  else root.dataset.theme = p;
}
apply(pref);

function setTheme(p: ThemePref) {
  pref = p;
  apply(p);
  try {
    if (p === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, p);
  } catch {
    /* private mode */
  }
  listeners.forEach((l) => l());
}

export function useTheme(): [ThemePref, (t: ThemePref) => void] {
  const value = useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => pref,
  );
  return [value, setTheme];
}
