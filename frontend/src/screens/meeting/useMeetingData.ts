import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, subscribeMeetings } from "../../api/client";
import type { Frame, Meeting, Moment, Notes, Stage, TranscriptLine } from "../../api/types";
import { patchMeeting } from "../../lib/meetings";

export interface MeetingData {
  meeting: Meeting | null;
  lines: TranscriptLine[];
  notes: Notes | null;
  frames: Frame[];
  moments: Moment[];
  error: string | null;
  notFound: boolean;
  loaded: boolean;
}

const stageDone = (stages: Stage[] | undefined, name: string) => stages?.find((s) => s.name === name)?.status === "done";

export function useMeetingData(id: string) {
  const [d, setD] = useState<MeetingData>({
    meeting: null,
    lines: [],
    notes: null,
    frames: [],
    moments: [],
    error: null,
    notFound: false,
    loaded: false,
  });
  const prevStages = useRef<Stage[] | undefined>(undefined);
  const lastTxFetch = useRef(0);

  const loadMeeting = useCallback(async () => {
    const meeting = await api.meeting(id);
    prevStages.current = meeting.stages;
    setD((x) => ({ ...x, meeting }));
    return meeting;
  }, [id]);
  const loadLines = useCallback(async () => {
    lastTxFetch.current = Date.now();
    const lines = await api.transcript(id);
    lines.sort((a, b) => a.start - b.start);
    setD((x) => ({ ...x, lines }));
  }, [id]);
  const loadNotes = useCallback(async () => {
    const notes = await api.notes(id);
    setD((x) => ({ ...x, notes }));
  }, [id]);
  const loadFrames = useCallback(async () => {
    const frames = await api.frames(id);
    frames.sort((a, b) => a.t - b.t);
    setD((x) => ({ ...x, frames }));
  }, [id]);
  const loadMoments = useCallback(async () => {
    const moments = await api.moments(id);
    moments.sort((a, b) => a.t - b.t);
    setD((x) => ({ ...x, moments }));
  }, [id]);

  const loadAll = useCallback(async () => {
    try {
      await loadMeeting();
      await Promise.allSettled([loadLines(), loadNotes(), loadFrames(), loadMoments()]);
      setD((x) => ({ ...x, loaded: true, error: null }));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setD((x) => ({ ...x, notFound: true, loaded: true }));
      else setD((x) => ({ ...x, error: e instanceof Error ? e.message : String(e), loaded: true }));
    }
  }, [loadMeeting, loadLines, loadNotes, loadFrames, loadMoments]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  // Live updates: merge stages, refetch what a finished stage produced.
  useEffect(() => {
    return subscribeMeetings((e) => {
      if (e.id !== id) return;
      if (e.deleted || e.status === "deleted") {
        setD((x) => ({ ...x, notFound: true }));
        return;
      }
      const before = prevStages.current;
      prevStages.current = e.stages;
      setD((x) => (x.meeting ? { ...x, meeting: { ...x.meeting, status: e.status, stages: e.stages?.length ? e.stages : x.meeting.stages, ...(e.title ? { title: e.title } : {}), ...(e.error !== undefined ? { error: e.error } : {}) } } : x));
      const finished = (name: string) => stageDone(e.stages, name) && !stageDone(before, name);
      if (finished("diarize") || finished("synthesize")) void loadMeeting().catch(() => {});
      if (finished("transcribe") || finished("diarize")) void loadLines().catch(() => {});
      if (finished("key_moments")) void loadMoments().catch(() => {});
      if (finished("frames")) void loadFrames().catch(() => {});
      if (finished("synthesize")) void loadNotes().catch(() => {});
      // A rerun resets stages to pending: drop the outputs it will replace.
      const reset = (name: string) => stageDone(before, name) && e.stages?.find((s) => s.name === name)?.status === "pending";
      if (reset("transcribe")) void loadLines().catch(() => {});
      if (reset("synthesize")) setD((x) => ({ ...x, notes: null }));
      if (reset("frames")) void loadFrames().catch(() => {});
      // While transcribing, refresh the growing transcript every few seconds.
      const tx = e.stages?.find((s) => s.name === "transcribe");
      if (tx?.status === "running" && Date.now() - lastTxFetch.current > 4000) void loadLines().catch(() => {});
    });
  }, [id, loadMeeting, loadLines, loadMoments, loadFrames, loadNotes]);

  const setMeeting = useCallback(
    (fn: (m: Meeting) => Meeting) => {
      setD((x) => {
        if (!x.meeting) return x;
        const m = fn(x.meeting);
        patchMeeting(m.id, { title: m.title, speakers: m.speakers });
        return { ...x, meeting: m };
      });
    },
    [],
  );
  const setLines = useCallback((fn: (l: TranscriptLine[]) => TranscriptLine[]) => setD((x) => ({ ...x, lines: fn(x.lines) })), []);

  return { ...d, reload: loadAll, reloadMeeting: loadMeeting, reloadLines: loadLines, setMeeting, setLines };
}
