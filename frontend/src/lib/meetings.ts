import { useCallback, useEffect, useSyncExternalStore } from "react";
import { api, subscribeMeetings } from "../api/client";
import type { Meeting, MeetingEvent } from "../api/types";

interface State {
  meetings: Meeting[];
  loaded: boolean;
  error: string | null;
}
let state: State = { meetings: [], loaded: false, error: null };
const ls = new Set<() => void>();
const set = (s: Partial<State>) => {
  state = { ...state, ...s };
  ls.forEach((l) => l());
};

let inflight: Promise<void> | null = null;
export function reloadMeetings(): Promise<void> {
  if (inflight) return inflight;
  inflight = api
    .meetings()
    .then((meetings) => set({ meetings, loaded: true, error: null }))
    .catch((e) => set({ loaded: true, error: e instanceof Error ? e.message : String(e) }))
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

export function applyEvent(e: MeetingEvent) {
  if (e.deleted || e.status === "deleted") {
    set({ meetings: state.meetings.filter((m) => m.id !== e.id) });
    return;
  }
  const idx = state.meetings.findIndex((m) => m.id === e.id);
  if (idx < 0) {
    void reloadMeetings();
    return;
  }
  const next = [...state.meetings];
  next[idx] = {
    ...next[idx],
    status: e.status,
    stages: e.stages ?? next[idx].stages,
    ...(e.title ? { title: e.title } : {}),
    ...(e.duration_s != null ? { duration_s: e.duration_s } : {}),
    ...(e.error !== undefined ? { error: e.error } : {}),
    ...(e.current_stage !== undefined ? { current_stage: e.current_stage } : {}),
    ...(e.progress != null ? { progress: e.progress } : {}),
  };
  set({ meetings: next });
}

export function patchMeeting(id: string, patch: Partial<Meeting>) {
  set({ meetings: state.meetings.map((m) => (m.id === id ? { ...m, ...patch } : m)) });
}
export function removeMeeting(id: string) {
  set({ meetings: state.meetings.filter((m) => m.id !== id) });
}

export function useMeetings() {
  const s = useSyncExternalStore(
    (l) => {
      ls.add(l);
      return () => ls.delete(l);
    },
    () => state,
  );
  useEffect(() => {
    if (!state.loaded) void reloadMeetings();
    return subscribeMeetings(applyEvent);
  }, []);
  const reload = useCallback(() => reloadMeetings(), []);
  return { ...s, reload };
}
