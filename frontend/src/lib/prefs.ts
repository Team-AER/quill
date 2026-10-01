import { useCallback, useState } from "react";

// Per-browser conveniences (layout, playback position, ticked action items).
// Storage can throw or be empty (private mode); everything falls back to defaults.

export function readPref<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(`quill.${key}`);
    return v == null ? fallback : (JSON.parse(v) as T);
  } catch {
    return fallback;
  }
}

export function writePref(key: string, value: unknown) {
  try {
    if (value == null) localStorage.removeItem(`quill.${key}`);
    else localStorage.setItem(`quill.${key}`, JSON.stringify(value));
  } catch {
    /* private mode or full */
  }
}

export function usePref<T>(key: string, fallback: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => readPref(key, fallback));
  const set = useCallback(
    (next: T) => {
      setV(next);
      writePref(key, next);
    },
    [key],
  );
  return [v, set];
}
