// Types mirror docs/CONTRACT.md. Fields the contract does not pin down are optional
// and normalised in client.ts so the UI tolerates small backend shape differences.

export type StageName =
  | "probe"
  | "extract_audio"
  | "diarize"
  | "transcribe"
  | "key_moments"
  | "frames"
  | "synthesize";

export const STAGE_ORDER: StageName[] = [
  "probe",
  "extract_audio",
  "diarize",
  "transcribe",
  "key_moments",
  "frames",
  "synthesize",
];

export const STAGE_LABEL: Record<string, string> = {
  probe: "Probe",
  extract_audio: "Audio",
  diarize: "Speakers",
  transcribe: "Transcribe",
  key_moments: "Key moments",
  frames: "Frames",
  synthesize: "Notes",
};

export type StageStatus = "pending" | "running" | "paused" | "failed" | "done" | "skipped";

export interface Stage {
  name: StageName | string;
  status: StageStatus | string;
  progress: number | null;
  detail?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
}

export type MeetingStatus = "queued" | "processing" | "running" | "paused" | "failed" | "done" | string;

export interface Speaker {
  label: string; // "S1".."S8"
  display_name: string | null;
  suggested_name?: string | null;
  suggestion_evidence_t?: number | null;
  color: number | null;
  /** Server-resolved name ("Speaker 2" when unnamed). */
  name?: string;
  /** Seconds of speech from diarization turns. */
  talk_time_s?: number | null;
}

export type Access = "owner" | "edit" | "view";
export type ShareLevel = "view" | "edit";

/** Someone on this Quill, as anyone signed in may see them (for sharing). */
export interface Teammate {
  id: number;
  name: string;
  email: string;
}

export interface Sharing {
  owner: Teammate | null;
  /** Shared with everyone on this Quill, and at which level. */
  everyone: ShareLevel | null;
  people: { user_id: number; name: string; email: string; access: ShareLevel; shared_at: string | null; disabled: boolean }[];
}

export interface Meeting {
  id: string;
  title: string;
  created_at: string;
  mode: "video" | "audio" | string;
  language?: string | null;
  expected_speakers?: number | null;
  duration_s: number | null;
  source_name?: string | null;
  source_bytes?: number | null;
  has_video?: number | boolean | null;
  width?: number | null;
  height?: number | null;
  status: MeetingStatus;
  error?: string | null;
  source_deleted_at?: string | null;
  stages?: Stage[];
  speakers?: Speaker[];
  speaker_count?: number | null;
  /** List endpoint: first unfinished stage and overall 0..1 progress. */
  current_stage?: string | null;
  progress?: number | null;
  has_notes?: boolean;
  /** List endpoint: best frame thumbnail, first TL;DR line, action item count. */
  cover_url?: string | null;
  gist?: string | null;
  action_count?: number | null;
  /** Your access: owner, or what it was shared with you as. */
  access?: Access;
  owner?: Teammate | null;
  everyone_access?: ShareLevel | null;
  /** Owner only: how many people it is shared with personally. */
  shared_with?: number;
}

export interface Interjection {
  speaker: string;
  start: number;
  end: number;
}

export interface TranscriptLine {
  id: number;
  speaker: string;
  start: number;
  end: number;
  text: string;
  overlap: number | boolean;
  segment_id?: number | null;
  interjections?: Interjection[] | null;
}

export interface Grounded {
  text: string;
  t: number | null;
  grounded: boolean;
}

export interface ActionItem {
  owner: string | null;
  task: string;
  due: string | null;
  t: number | null;
  grounded: boolean;
}

export interface Chapter {
  title: string;
  start: number;
  end: number;
  summary: string;
}

export interface Notes {
  tldr: string[];
  summary: string;
  chapters: Chapter[];
  decisions: Grounded[];
  action_items: ActionItem[];
  open_questions: Grounded[];
  key_visuals: { frame_id: number; t: number; caption: string }[];
  speaker_suggestions: { label: string; name: string; evidence_t: number }[];
}

export interface Frame {
  id: number;
  moment_id?: number | null;
  t: number;
  kind: string | null;
  title: string | null;
  visible_text: string | null;
  key_facts: string[];
  relevance: number | null;
  caption: string | null;
  image_url?: string;
  thumb_url?: string;
}

export interface Moment {
  id: number;
  t: number;
  source: string | null;
  why: string | null;
  look_for?: string | null;
}

export type Role = "admin" | "member";

export interface User {
  id: number;
  email: string;
  /** May be empty; use displayName() */
  name: string;
  is_admin: boolean;
  role: Role;
  created_at?: string | null;
}

/** A person as the admin People list sees them. */
export interface Person extends User {
  disabled: boolean;
  disabled_at: string | null;
  last_seen_at: string | null;
  invited_by: string | null;
  meeting_count: number;
  uploaded_bytes: number;
  session_count: number;
}

export interface Session {
  id: string;
  created_at: string | null;
  last_seen_at: string | null;
  user_agent: string;
  ip: string;
  current: boolean;
}

export interface InvitePreview {
  /** null: an open link, the invitee picks the email */
  email: string | null;
  name: string;
  is_admin: boolean;
  role: Role;
  invited_by: string;
  expires_at: string;
}

export interface ResetPreview {
  email: string;
  name: string;
  expires_at: string;
}

/** A one-time link to hand over (invite or password reset), with what it is for. */
export interface ShareableLink {
  url: string;
  expires_at: string;
  email: string | null;
  name: string;
}

export type ModelStatus = "ready" | "degraded" | "disabled" | "quarantined" | "unknown" | string;

export interface ModelInfo {
  model: string;
  status: ModelStatus;
}

export interface SystemInfo {
  stt: ModelInfo | null;
  vision: ModelInfo | null;
  text: ModelInfo | null;
  disk_free_bytes: number | null;
  disk_total_bytes: number | null;
  max_upload_bytes: number | null;
  worker: { heartbeat_at: string | null; queue_length: number | null; meeting_id: string | null; stage: string | null } | null;
}

export interface SttOption extends ModelInfo {
  selectable: boolean;
}

export interface Settings {
  stt_model: string;
  text_model?: string;
  vision_model?: string;
  /** -1 = keep forever, 0 = delete after processing, N = days */
  video_retention_days: number;
  stt_model_options: SttOption[];
}

export type InviteStatus = "pending" | "expired" | "used" | "revoked";

export interface Invite {
  id: string;
  email: string | null;
  name: string;
  is_admin: boolean;
  role: Role;
  created_at: string | null;
  expires_at: string;
  used_at: string | null;
  revoked_at: string | null;
  status: InviteStatus;
  invited_by: string | null;
  used_by: string | null;
  /** Only right after creating or renewing it: the token is never stored. */
  url?: string;
}

export interface SearchTranscriptHit {
  meeting_id: string;
  meeting_title?: string | null;
  line_id?: number | null;
  speaker?: string | null;
  speaker_name?: string | null;
  start: number;
  end?: number;
  text: string;
  /** Plain text with matches wrapped in [[ ]]. */
  snippet?: string | null;
}

export interface SearchFrameHit {
  meeting_id: string;
  meeting_title?: string | null;
  frame_id: number;
  t: number;
  title?: string | null;
  caption?: string | null;
  visible_text?: string | null;
  snippet?: string | null;
  thumb_url?: string | null;
}

export interface SearchResults {
  transcript: SearchTranscriptHit[];
  frames: SearchFrameHit[];
}

export interface MeetingEvent {
  id: string;
  status: MeetingStatus;
  stages: Stage[];
  error?: string | null;
  title?: string;
  mode?: string;
  duration_s?: number | null;
  current_stage?: string | null;
  progress?: number | null;
  /** Older mock shape; the API sends status "deleted". */
  deleted?: boolean;
}

export const EXPORT_FORMATS = [
  { id: "md", label: "Notes (Markdown)" },
  { id: "txt", label: "Transcript (TXT)" },
  { id: "srt", label: "Subtitles (SRT)" },
  { id: "vtt", label: "Subtitles (VTT)" },
  { id: "json", label: "Everything (JSON)" },
  { id: "rttm", label: "Diarization (RTTM)" },
] as const;
