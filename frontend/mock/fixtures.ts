// Deterministic fixture data for the dev mock. No network, no randomness across runs.

export interface MLine {
  id: number;
  speaker: string;
  start: number;
  end: number;
  text: string;
  overlap: number;
  segment_id: number;
  interjections: { speaker: string; start: number; end: number }[] | null;
}
export interface MSpeaker {
  label: string;
  display_name: string | null;
  suggested_name: string | null;
  suggestion_evidence_t: number | null;
  color: number;
}
export interface MStage {
  name: string;
  status: string;
  progress: number | null;
  detail: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}
export interface MFrame {
  id: number;
  moment_id: number;
  t: number;
  kind: string;
  title: string;
  visible_text: string;
  key_facts: string[];
  relevance: number;
  caption: string;
  hue: number;
}
export interface MMoment {
  id: number;
  t: number;
  source: string;
  why: string;
  look_for: string;
}
export interface MMeeting {
  id: string;
  owner_id: number;
  title: string;
  created_at: string;
  mode: "video" | "audio";
  language: string;
  expected_speakers: number | null;
  duration_s: number;
  source_name: string;
  source_bytes: number;
  has_video: number;
  width: number | null;
  height: number | null;
  status: string;
  error: string | null;
  source_deleted_at: string | null;
  stages: MStage[];
  speakers: MSpeaker[];
  lines: MLine[];
  frames: MFrame[];
  moments: MMoment[];
  notes: unknown | null;
  /** Data the simulator reveals when the owning stage finishes. */
  _final?: Partial<Pick<MMeeting, "lines" | "speakers" | "frames" | "moments" | "notes">>;
}

export const STAGES = ["probe", "extract_audio", "diarize", "transcribe", "key_moments", "frames", "synthesize"];

function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Topic {
  title: string;
  share: number; // fraction of duration
  summary: string;
  sentences: string[];
}

const FILLERS = [
  "Right.",
  "Yeah, exactly.",
  "Mm-hm, go on.",
  "Okay, that makes sense.",
  "Sorry, can you repeat that last part?",
  "Good point.",
  "Agreed.",
  "Let me just share my screen for a second.",
];

const ROADMAP_TOPICS: Topic[] = [
  {
    title: "Welcome and agenda",
    share: 0.07,
    summary: "Priya opens, introduces Daniel from infra, and lists the four agenda items.",
    sentences: [
      "Morning everyone, thanks for joining, let's give people one more minute.",
      "I'm Priya, I'll run this one; Daniel from infra is joining us today for the storage discussion.",
      "Hi all, Daniel Okafor here, I look after the Proxmox side and the backups.",
      "The agenda is Q3 numbers, the upload pipeline, diarization on CPU, and then budget.",
      "Mei, you had the Q3 retro deck ready, right?",
      "We have a hard stop at the top of the hour, so let's keep each item to about fifteen minutes.",
      "Tomás, can you take notes on owners as we go?",
    ],
  },
  {
    title: "Q3 retro: usage and reliability",
    share: 0.18,
    summary: "Weekly active users grew 38 % quarter over quarter; two incidents traced to disk pressure on the media volume.",
    sentences: [
      "As you can see on this slide, weekly actives went from about four hundred to five hundred and fifty.",
      "That's a thirty-eight percent increase quarter over quarter, mostly from the design team onboarding.",
      "The two incidents in August were both disk pressure on the media volume, not the API itself.",
      "Median processing time for a one hour meeting was twenty-two minutes, with the tail at almost an hour.",
      "The tail is almost entirely STT queueing behind other GPU work on Avifors.",
      "We should alert at eighty percent disk, not ninety-five; ninety-five is already too late.",
      "Error rate on uploads dropped once we moved to resumable chunks.",
      "The chart on the right shows retries per upload, and it's basically flat since the tus switch.",
      "I'd like to see that broken out by client network, office versus home.",
    ],
  },
  {
    title: "Upload pipeline and storage",
    share: 0.22,
    summary: "Team agrees on 64 MB tus chunks, a 10 GiB cap and a 14-day source retention default.",
    sentences: [
      "The proposal is sixty-four megabyte chunks through NPMplus with request buffering off.",
      "Anything bigger than that and a dropped Wi-Fi connection costs too much to redo.",
      "We keep the source video for fourteen days by default, then only the audio and frames.",
      "Can we make retention a per-instance setting rather than hard coding it?",
      "Yes, it's already an environment variable, the admin page just needs to expose it.",
      "What happens if someone uploads a ten gigabyte file and the disk only has eight free?",
      "We reserve the space before accepting the upload, so it fails fast with a clear message.",
      "Let me pull up the storage dashboard so everyone can see the current headroom.",
      "We're at about sixty percent on the three hundred gig volume right now.",
      "I'd rather over-provision now than migrate the LXC under load later.",
    ],
  },
  {
    title: "Diarization on CPU",
    share: 0.22,
    summary: "Nemotron runs at roughly 7x real time on 8 cores; ONNX export is the fallback if the gate is missed.",
    sentences: [
      "The benchmark came back this morning, Nemotron on eight cores does about seven times real time.",
      "So a one hour meeting diarizes in under nine minutes, which clears the gate.",
      "Memory peaked at around five gigabytes, so sixteen for the container is comfortable.",
      "Here's the flame graph from the run, most of the time is in the encoder attention.",
      "If we miss the gate on longer meetings, the ONNX export is the plan B.",
      "How does it handle more than eight speakers?",
      "It has eight slots, so very large meetings will merge similar voices; we warn when all eight are in use.",
      "I tested crosstalk on the design review recording and overlap detection looked right.",
      "We should keep the median filter, without it we get a lot of tiny flickering turns.",
      "Mei, could you write up the threshold settings so they're not only in my notebook?",
    ],
  },
  {
    title: "Budget and GPU sharing",
    share: 0.17,
    summary: "No new GPU spend this quarter; Quill shares Gemma with Hedwig and Erised at low concurrency.",
    sentences: [
      "On budget, the short answer is no new GPU spend this quarter.",
      "Gemma is already warm for Hedwig and Erised, so Quill shares it at concurrency two.",
      "The table on this slide shows the projected GPU hours per week, we're at forty percent.",
      "If summaries start timing out, Qwen is the fallback, same as Hedwig does it.",
      "Do we have a number for what a dedicated card would cost us per month?",
      "Roughly, but I'd rather not put it in the notes until finance confirms.",
      "Let's revisit this in December with real usage data.",
    ],
  },
  {
    title: "Wrap-up and owners",
    share: 0.14,
    summary: "Owners assigned for alerting, retention setting, threshold write-up and the December budget review.",
    sentences: [
      "Okay, let's go through owners before we drop.",
      "Daniel takes the disk alert change, target end of next week.",
      "Tomás owns exposing retention in the admin settings.",
      "Mei writes up the diarization thresholds, and I'll review it.",
      "I'll set up the December budget review and send the invite.",
      "Anything we didn't get to? The mobile upload question, we'll push that to next time.",
      "Great, thanks everyone, that was a productive one.",
    ],
  },
];

const DESIGN_TOPICS: Topic[] = [
  {
    title: "Reader layout",
    share: 0.35,
    summary: "The glass rail stays; content sheets become opaque for legibility.",
    sentences: [
      "The main feedback was that text on glass is hard to read over a whole day.",
      "So the rail and toolbars stay frosted and the reading surface becomes opaque.",
      "Fourteen pixel radii on sheets, eight on controls, nothing bigger.",
      "I like it, it feels closer to Apple Mail without copying it.",
      "What about the summary block at the top of a thread?",
      "It stays, but clamped to three lines with a show more link.",
    ],
  },
  {
    title: "Type and icons",
    share: 0.35,
    summary: "System font stack only; 16 px line icons with tooltips naming the shortcut.",
    sentences: [
      "We drop the serif entirely, system stack everywhere.",
      "Figures use tabular numerals instead of the mono font.",
      "Every standard action becomes an icon with a tooltip that names the key.",
      "Text labels stay only for navigation and destructive confirmations.",
      "Can we get the icon set as a single file so it's easy to extend?",
    ],
  },
  {
    title: "Next steps",
    share: 0.3,
    summary: "Build the token test and ship the rail first.",
    sentences: [
      "Let's add a contrast test that composites the glass over the field.",
      "Rail first, then the list rows, then the reader toolbar.",
      "I'll pair with you on the dark theme tokens tomorrow.",
      "Sounds like a plan, thanks both.",
    ],
  },
];

const ACME_TOPICS: Topic[] = [
  {
    title: "Onboarding status",
    share: 0.5,
    summary: "Acme has 40 of 120 seats active; SSO is the blocker.",
    sentences: [
      "Thanks for making time, I know it's late on your side.",
      "Forty of the hundred and twenty seats are active so far.",
      "The blocker is SSO, our IT team needs the SAML metadata.",
      "I'll send that over today, it's a two minute export on our end.",
      "Can you show the admin screen where seats get assigned?",
    ],
  },
  {
    title: "Data residency",
    share: 0.5,
    summary: "Acme asks where recordings are stored and for how long.",
    sentences: [
      "Where exactly are the recordings stored, and for how long?",
      "Everything stays on our own hardware, and source video is deleted after fourteen days.",
      "Transcripts stay until you delete them.",
      "That works for legal, as long as we can export everything first.",
    ],
  },
];

function genLines(
  seed: number,
  duration: number,
  speakerCount: number,
  topics: Topic[],
  maxLines: number,
): { lines: MLine[]; chapters: { title: string; start: number; end: number; summary: string }[] } {
  const r = rng(seed);
  const lines: MLine[] = [];
  const chapters: { title: string; start: number; end: number; summary: string }[] = [];
  const avg = duration / maxLines;
  let t = 1.2;
  let speaker = 0;
  let id = 1;
  let chStart = 0;
  topics.forEach((topic, ti) => {
    const chEnd = ti === topics.length - 1 ? duration : chStart + topic.share * duration;
    chapters.push({ title: topic.title, start: Math.round(chStart), end: Math.round(chEnd), summary: topic.summary });
    let si = 0;
    while (t < chEnd - 2 && lines.length < maxLines) {
      // Pick next speaker: hosts talk more (S1 weight highest)
      const change = r() < 0.72;
      if (change) {
        const weights = Array.from({ length: speakerCount }, (_, i) => (i === speaker ? 0 : i === 0 ? 3 : 2 - i * 0.2));
        const sum = weights.reduce((a, b) => a + b, 0);
        let x = r() * sum;
        for (let i = 0; i < speakerCount; i++) {
          x -= weights[i];
          if (x <= 0) {
            speaker = i;
            break;
          }
        }
      }
      let text: string;
      if (r() < 0.12) text = FILLERS[Math.floor(r() * FILLERS.length)];
      else {
        const n = 1 + Math.floor(r() * 2.2);
        const parts: string[] = [];
        for (let k = 0; k < n; k++) parts.push(topic.sentences[(si++ + Math.floor(r() * 3)) % topic.sentences.length]);
        text = [...new Set(parts)].join(" ");
      }
      const len = Math.max(1.4, Math.min(avg * 2.2, text.length / 14 + r() * 3));
      const start = t;
      const end = Math.min(chEnd, start + len);
      const overlap = r() < 0.05 ? 1 : 0;
      let interjections: MLine["interjections"] = null;
      if (r() < 0.09 && end - start > 4) {
        const other = (speaker + 1 + Math.floor(r() * (speakerCount - 1))) % speakerCount;
        const is = start + (end - start) * (0.3 + r() * 0.4);
        interjections = [{ speaker: `S${other + 1}`, start: +is.toFixed(2), end: +(is + 0.6).toFixed(2) }];
      }
      lines.push({
        id: seed * 10000 + id,
        speaker: `S${speaker + 1}`,
        start: +start.toFixed(2),
        end: +end.toFixed(2),
        text,
        overlap,
        segment_id: id,
        interjections,
      });
      id++;
      const gap = overlap ? -0.4 : 0.2 + r() * Math.max(0.5, avg - len);
      t = Math.max(end + gap, start + 0.8);
    }
    t = Math.max(t, chEnd + 0.5);
    chStart = chEnd;
  });
  return { lines, chapters };
}

function iso(minsAgo: number): string {
  return new Date(Date.UTC(2026, 8, 30, 9, 0, 0) - minsAgo * 60000).toISOString();
}

export function stagesFor(mode: "video" | "audio", done: number, current?: Partial<MStage>): MStage[] {
  return STAGES.map((name, i) => {
    const skipped = mode === "audio" && (name === "key_moments" || name === "frames");
    if (skipped) return { name, status: "skipped", progress: null, detail: null, started_at: null, finished_at: null, error: null };
    if (i < done)
      return { name, status: "done", progress: 1, detail: null, started_at: iso(60 - i * 2), finished_at: iso(58 - i * 2), error: null };
    if (i === done && current)
      return {
        name,
        status: current.status ?? "running",
        progress: current.progress ?? 0,
        detail: current.detail ?? null,
        started_at: current.started_at ?? new Date(Date.now() - 90_000).toISOString(),
        finished_at: null,
        error: current.error ?? null,
      };
    return { name, status: "pending", progress: 0, detail: null, started_at: null, finished_at: null, error: null };
  });
}

function speakersFor(names: (string | null)[], suggestions: (string | null)[], evidence: (number | null)[]): MSpeaker[] {
  return names.map((n, i) => ({
    label: `S${i + 1}`,
    display_name: n,
    suggested_name: suggestions[i] ?? null,
    suggestion_evidence_t: evidence[i] ?? null,
    color: i,
  }));
}

const FRAME_SPECS: { kind: string; title: string; text: string[]; facts: string[]; caption: string; rel: number }[] = [
  { kind: "slide", title: "Agenda", text: ["1. Q3 numbers", "2. Upload pipeline", "3. Diarization on CPU", "4. Budget"], facts: ["Four agenda items"], caption: "Agenda slide with the four topics for the sync.", rel: 1 },
  { kind: "slide", title: "Q3 weekly active users", text: ["Jul 402", "Aug 471", "Sep 556", "+38% QoQ"], facts: ["WAU 402 → 556", "+38 % quarter over quarter"], caption: "Bar chart of weekly active users rising from 402 to 556.", rel: 3 },
  { kind: "slide", title: "Incidents", text: ["Aug 12  media volume 97% full", "Aug 29  media volume 99% full", "Root cause: no retention sweep"], facts: ["Both incidents were disk pressure"], caption: "Incident timeline: both August outages were disk pressure.", rel: 3 },
  { kind: "screen", title: "Upload retries per file", text: ["retries/upload", "before tus: 2.4", "after tus: 0.1"], facts: ["Retries dropped from 2.4 to 0.1 after tus"], caption: "Dashboard showing upload retries flattening after the tus switch.", rel: 2 },
  { kind: "slide", title: "Upload design", text: ["tus 1.0, 64 MB chunks", "10 GiB cap", "reserve disk before accept", "sha256 at completion"], facts: ["64 MB chunks", "10 GiB cap"], caption: "Upload design: tus with 64 MB chunks and a 10 GiB cap.", rel: 3 },
  { kind: "screen", title: "Storage dashboard", text: ["/var/lib/quill", "used 181 GiB / 300 GiB", "60%"], facts: ["Media volume at 60 %"], caption: "Storage dashboard: 181 of 300 GiB used.", rel: 2 },
  { kind: "whiteboard", title: "Retention", text: ["source video → 14 d", "audio / transcript → until deleted", "frames → until deleted"], facts: ["Source retention 14 days"], caption: "Whiteboard sketch of the retention policy.", rel: 2 },
  { kind: "slide", title: "Nemotron CPU benchmark", text: ["8 cores  7.1× RT", "1 h → 8m 27s", "peak RSS 5.1 GB"], facts: ["7.1× real time on 8 cores", "Peak memory 5.1 GB"], caption: "Benchmark table: 7.1× real time, 5.1 GB peak memory.", rel: 3 },
  { kind: "demo", title: "Flame graph", text: ["encoder.attention 61%", "conv subsampling 14%", "post-proc 3%"], facts: ["61 % of time in encoder attention"], caption: "Flame graph of the diarizer run; attention dominates.", rel: 1 },
  { kind: "slide", title: "GPU hours per week", text: ["Hedwig 31 h", "Erised 12 h", "Quill (proj.) 9 h", "capacity 130 h"], facts: ["Projected utilisation 40 %"], caption: "Table of weekly GPU hours per app against capacity.", rel: 3 },
  { kind: "people", title: "", text: [], facts: [], caption: "Gallery view of the four participants.", rel: 0 },
  { kind: "slide", title: "Owners", text: ["Daniel  disk alert @80%", "Tomás  retention setting", "Mei  threshold write-up", "Priya  Dec budget review"], facts: ["Four owners assigned"], caption: "Owners slide summarising who does what.", rel: 3 },
];

function framesFor(lines: MLine[], chapters: { start: number; end: number }[], seed: number) {
  const frames: MFrame[] = [];
  const moments: MMoment[] = [];
  // two frames per chapter, spread
  const perCh = [1, 3, 3, 2, 2, 1];
  let fi = 0;
  chapters.forEach((ch, ci) => {
    for (let k = 0; k < perCh[ci] && fi < FRAME_SPECS.length; k++) {
      const t = Math.round(ch.start + ((k + 1) * (ch.end - ch.start)) / (perCh[ci] + 1));
      const spec = FRAME_SPECS[fi];
      const near = lines.find((l) => l.start >= t - 30 && /slide|screen|see|dashboard|graph|table|chart/i.test(l.text));
      moments.push({
        id: seed * 100 + fi + 1,
        t,
        source: near ? "text+scene" : "scene",
        why: near ? `Speaker refers to on-screen content: “${near.text.slice(0, 60)}…”` : "Scene change held for over 30 s",
        look_for: spec.title || "participants",
      });
      frames.push({
        id: seed * 100 + fi + 1,
        moment_id: seed * 100 + fi + 1,
        t,
        kind: spec.kind,
        title: spec.title,
        visible_text: spec.text.join("\n"),
        key_facts: spec.facts,
        relevance: spec.rel,
        caption: spec.caption,
        hue: (fi * 37 + 200) % 360,
      });
      fi++;
    }
  });
  moments.push({ id: seed * 100 + 50, t: 1422, source: "text", why: "Decision: 14-day retention default", look_for: "" });
  moments.push({ id: seed * 100 + 51, t: 3380, source: "text", why: "Owners confirmed", look_for: "" });
  moments.sort((a, b) => a.t - b.t);
  return { frames, moments };
}

function tAt(lines: MLine[], needle: string): number {
  return lines.find((l) => l.text.includes(needle))?.start ?? 0;
}

function roadmapNotes(lines: MLine[], chapters: ReturnType<typeof genLines>["chapters"], frames: MFrame[]) {
  return {
    tldr: [
      "Weekly actives grew 38 % in Q3; both August incidents were disk pressure on the media volume.",
      "Uploads go through tus with 64 MB chunks, a 10 GiB cap and 14-day source retention.",
      "Nemotron diarizes 1 h of audio in under 9 min on 8 cores, clearing the CPU gate.",
      "No new GPU spend this quarter; Quill shares Gemma with Hedwig and Erised.",
    ],
    summary:
      "The team reviewed Q3, agreed the upload and storage design, confirmed the CPU diarization benchmark and deferred GPU budget.\n\n" +
      "**Q3.** Weekly actives rose from 402 to 556. Both incidents in August traced to the media volume filling up; alerting moves to **80 %**.\n\n" +
      "**Uploads.** Resumable tus uploads with 64 MB chunks through NPMplus (request buffering off). Disk is reserved before an upload is accepted. " +
      "Source video is kept for **14 days** by default, now an admin setting.\n\n" +
      "**Diarization.** Nemotron runs at about 7× real time on 8 cores with 5 GB peak memory. ONNX export is the fallback if longer meetings miss the gate.\n\n" +
      "**Budget.** No dedicated GPU this quarter. Revisit in December with real usage:\n\n" +
      "- concurrency 2 on shared Gemma\n- Qwen as the summary fallback\n",
    chapters,
    decisions: [
      { text: "Upload chunks are 64 MB via tus, with a 10 GiB hard cap.", t: tAt(lines, "sixty-four megabyte"), grounded: true },
      { text: "Source video retention defaults to 14 days and becomes an admin setting.", t: tAt(lines, "fourteen days by default"), grounded: true },
      { text: "Disk alerts fire at 80 % instead of 95 %.", t: tAt(lines, "eighty percent disk"), grounded: true },
      { text: "No new GPU spend this quarter.", t: tAt(lines, "no new GPU spend"), grounded: true },
    ],
    action_items: [
      { owner: "S2", task: "Change the media-volume disk alert threshold to 80 %.", due: "2026-10-09", t: tAt(lines, "disk alert change"), grounded: true },
      { owner: "S4", task: "Expose video retention days in the admin settings page.", due: null, t: tAt(lines, "exposing retention"), grounded: true },
      { owner: "S3", task: "Write up the diarization threshold and median-filter settings.", due: null, t: tAt(lines, "thresholds"), grounded: true },
      { owner: "Priya", task: "Schedule the December budget review and send the invite.", due: "2026-12-01", t: tAt(lines, "December budget review"), grounded: true },
      { owner: "S2", task: "Break out upload retries by client network (office vs home).", due: null, t: tAt(lines, "broken out by client network"), grounded: true },
      { owner: null, task: "Get a monthly cost quote for a dedicated GPU from finance.", due: null, t: null, grounded: false },
    ],
    open_questions: [
      { text: "How should very large meetings (more than 8 speakers) be handled beyond a warning?", t: tAt(lines, "more than eight speakers"), grounded: true },
      { text: "Is mobile upload in scope for v1?", t: tAt(lines, "mobile upload"), grounded: true },
      { text: "What does a dedicated GPU cost per month?", t: tAt(lines, "dedicated card"), grounded: true },
    ],
    key_visuals: frames.filter((f) => f.relevance >= 3).map((f) => ({ frame_id: f.id, t: f.t, caption: f.caption })),
    speaker_suggestions: [
      { label: "S2", name: "Daniel Okafor", evidence_t: tAt(lines, "Daniel Okafor here") },
      { label: "S3", name: "Mei", evidence_t: tAt(lines, "Mei, you had") },
    ],
  };
}

function simpleNotes(chapters: ReturnType<typeof genLines>["chapters"], lines: MLine[], topic: string) {
  return {
    tldr: chapters.map((c) => c.summary),
    summary: `A short ${topic} session.\n\n` + chapters.map((c) => `**${c.title}.** ${c.summary}`).join("\n\n"),
    chapters,
    decisions: [{ text: chapters[0].summary, t: lines[3]?.start ?? 0, grounded: true }],
    action_items: [
      { owner: "S1", task: "Follow up on the items discussed.", due: null, t: lines[lines.length - 4]?.start ?? 0, grounded: true },
      { owner: "S2", task: "Share the updated draft with the group.", due: null, t: lines[lines.length - 2]?.start ?? 0, grounded: true },
    ],
    open_questions: [],
    key_visuals: [],
    speaker_suggestions: [],
  };
}

export function buildFixtures(): MMeeting[] {
  // 1. The flagship: 1 h, 4 speakers, ~300 lines, 12 frames.
  const r1 = genLines(1, 3600, 4, ROADMAP_TOPICS, 300);
  const f1 = framesFor(r1.lines, r1.chapters, 1);
  const m1: MMeeting = {
    id: "m_roadmap",
    owner_id: 1,
    title: "Q4 platform roadmap sync",
    created_at: iso(60 * 3),
    mode: "video",
    language: "en",
    expected_speakers: 4,
    duration_s: 3600,
    source_name: "2026-09-30 roadmap sync.mp4",
    source_bytes: 3_412_000_000,
    has_video: 1,
    width: 1920,
    height: 1080,
    status: "done",
    error: null,
    source_deleted_at: null,
    stages: stagesFor("video", 7),
    speakers: speakersFor(
      ["Priya Raman", null, null, null],
      [null, "Daniel Okafor", "Mei", null],
      [null, tAt(r1.lines, "Daniel Okafor here"), tAt(r1.lines, "Mei, you had"), null],
    ),
    lines: r1.lines,
    frames: f1.frames,
    moments: f1.moments,
    notes: null,
  };
  m1.notes = roadmapNotes(r1.lines, r1.chapters, f1.frames);

  // 2. Audio-mode meeting, finished.
  const r2 = genLines(2, 1800, 3, DESIGN_TOPICS, 190);
  const m2: MMeeting = {
    id: "m_design",
    owner_id: 1,
    title: "Design review: Hedwig reader",
    created_at: iso(60 * 26),
    mode: "audio",
    language: "en",
    expected_speakers: 3,
    duration_s: 1800,
    source_name: "design-review.m4a",
    source_bytes: 28_800_000,
    has_video: 0,
    width: null,
    height: null,
    status: "done",
    error: null,
    source_deleted_at: null,
    stages: stagesFor("audio", 7),
    speakers: speakersFor(["Anna", "Leo", null], [null, null, "Sam"], [null, null, 60]),
    lines: r2.lines,
    frames: [],
    moments: [],
    notes: simpleNotes(r2.chapters, r2.lines, "design review"),
  };

  // 3. Paused at STT because the model is disabled in llm-proxy.
  const r3 = genLines(3, 2400, 2, ACME_TOPICS, 90);
  const m3: MMeeting = {
    id: "m_acme",
    owner_id: 1,
    title: "Customer call: Acme onboarding",
    created_at: iso(45),
    mode: "video",
    language: "auto",
    expected_speakers: 2,
    duration_s: 2400,
    source_name: "acme-onboarding.mov",
    source_bytes: 1_900_000_000,
    has_video: 1,
    width: 1280,
    height: 720,
    status: "paused",
    error: null,
    source_deleted_at: null,
    stages: stagesFor("video", 3, {
      status: "paused",
      progress: 0.42,
      detail: "STT paused: model disabled in llm-proxy",
    }),
    speakers: speakersFor([null, null], [null, null], [null, null]),
    lines: r3.lines.slice(0, 38),
    frames: [],
    moments: [],
    notes: null,
    _final: { lines: r3.lines, notes: simpleNotes(r3.chapters, r3.lines, "customer call"), frames: [], moments: [] },
  };

  // 4. Failed at diarize.
  const m4: MMeeting = {
    id: "m_allhands",
    owner_id: 1,
    title: "September all-hands",
    created_at: iso(60 * 50),
    mode: "video",
    language: "en",
    expected_speakers: null,
    duration_s: 5400,
    source_name: "allhands-sep.mp4",
    source_bytes: 6_700_000_000,
    has_video: 1,
    width: 1920,
    height: 1080,
    status: "failed",
    error: "Diarizer exited with code 137 (out of memory)",
    source_deleted_at: null,
    stages: stagesFor("video", 2, {
      status: "failed",
      progress: 0.61,
      error: "Diarizer exited with code 137 (out of memory)",
    }),
    speakers: [],
    lines: [],
    frames: [],
    moments: [],
    notes: null,
    _final: (() => {
      const r4 = genLines(4, 5400, 6, ROADMAP_TOPICS, 420);
      return {
        lines: r4.lines,
        speakers: speakersFor(["Priya Raman", null, null, null, null, null], [], []),
        frames: [],
        moments: [],
        notes: simpleNotes(r4.chapters, r4.lines, "all-hands"),
      };
    })(),
  };

  // 5. Running right now (frames stage), progresses live through SSE.
  const r5 = genLines(5, 2700, 3, ROADMAP_TOPICS.slice(1, 4), 160);
  const f5 = framesFor(r5.lines, r5.chapters, 5);
  const m5: MMeeting = {
    id: "m_weekly",
    owner_id: 1,
    title: "Infra weekly",
    created_at: iso(12),
    mode: "video",
    language: "en",
    expected_speakers: 3,
    duration_s: 2700,
    source_name: "infra-weekly.mp4",
    source_bytes: 2_100_000_000,
    has_video: 1,
    width: 1920,
    height: 1080,
    status: "running",
    error: null,
    source_deleted_at: null,
    stages: stagesFor("video", 5, { status: "running", progress: 0.3, detail: "Analysing frame 4 of 12" }),
    speakers: speakersFor([null, null, null], [null, null, null], [null, null, null]),
    lines: r5.lines,
    frames: f5.frames.slice(0, 3),
    moments: f5.moments,
    notes: null,
  };
  m5._final = { frames: f5.frames, notes: simpleNotes(r5.chapters, r5.lines, "infra weekly") };

  // 6. Old source-deleted meeting (serves audio in place of the video).
  const r6 = genLines(6, 1500, 2, DESIGN_TOPICS.slice(0, 2), 80);
  const m6: MMeeting = {
    id: "m_retro",
    owner_id: 1,
    title: "Sprint 38 retro",
    created_at: iso(60 * 24 * 20),
    mode: "video",
    language: "en",
    expected_speakers: 2,
    duration_s: 1500,
    source_name: "retro-38.mp4",
    source_bytes: 900_000_000,
    has_video: 1,
    width: 1920,
    height: 1080,
    status: "done",
    error: null,
    source_deleted_at: iso(60 * 24 * 6),
    stages: stagesFor("video", 7),
    speakers: speakersFor(["Anna", "Leo"], [null, null], [null, null]),
    lines: r6.lines,
    frames: [],
    moments: [],
    notes: simpleNotes(r6.chapters, r6.lines, "retro"),
  };

  return [m5, m3, m1, m2, m4, m6];
}

/** Build a newly uploaded meeting that the simulator will walk through every stage. */
export function newUploadedMeeting(id: string, meta: Record<string, string>, size: number): MMeeting {
  const audioOnly = meta.audio_only === "true" || meta.audio_only === "1" || /\.(m4a|mp3|wav|flac|ogg|opus)$/i.test(meta.filename ?? "");
  const mode = audioOnly ? "audio" : "video";
  const dur = 900 + (size % 1800);
  const nSpk = Number(meta.expected_speakers) || 3;
  const r = genLines(Number(id.replace(/\D/g, "")) || 9, dur, Math.min(8, Math.max(2, nSpk)), DESIGN_TOPICS, Math.round(dur / 12));
  const f = framesFor(r.lines, r.chapters, 9);
  return {
    id,
    owner_id: 1,
    title: meta.title || (meta.filename ?? "Untitled").replace(/\.[^.]+$/, ""),
    created_at: new Date().toISOString(),
    mode,
    language: meta.language || "auto",
    expected_speakers: Number(meta.expected_speakers) || null,
    duration_s: dur,
    source_name: meta.filename ?? "upload",
    source_bytes: size,
    has_video: mode === "video" ? 1 : 0,
    width: mode === "video" ? 1920 : null,
    height: mode === "video" ? 1080 : null,
    status: "queued",
    error: null,
    source_deleted_at: null,
    stages: stagesFor(mode, 0),
    speakers: [],
    lines: [],
    frames: [],
    moments: [],
    notes: null,
    _final: {
        lines: r.lines,
        speakers: speakersFor(
          Array.from({ length: Math.min(8, Math.max(2, nSpk)) }, () => null),
          [],
          [],
        ),
        frames: mode === "video" ? f.frames.slice(0, 4) : [],
        moments: mode === "video" ? f.moments.slice(0, 4) : [],
        notes: simpleNotes(r.chapters, r.lines, "new"),
    },
  };
}

// ---------- placeholder frame images ----------
function esc(s: string) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}

export function frameSvg(f: MFrame, index: number): string {
  const W = 1280;
  const H = 720;
  const mm = Math.floor(f.t / 60);
  const ss = String(Math.floor(f.t % 60)).padStart(2, "0");
  const bg = `hsl(${f.hue} 35% 96%)`;
  const accent = `hsl(${f.hue} 60% 42%)`;
  if (f.kind === "people") {
    const tiles = [0, 1, 2, 3]
      .map((i) => {
        const x = 40 + (i % 2) * 610;
        const y = 40 + Math.floor(i / 2) * 330;
        const h = (f.hue + i * 70) % 360;
        return `<rect x="${x}" y="${y}" width="590" height="310" rx="14" fill="hsl(${h} 25% 30%)"/><circle cx="${x + 295}" cy="${y + 130}" r="60" fill="hsl(${h} 30% 70%)"/><rect x="${x + 205}" y="${y + 200}" width="180" height="90" rx="45" fill="hsl(${h} 30% 70%)"/>`;
      })
      .join("");
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"><rect width="${W}" height="${H}" fill="#111"/>${tiles}</svg>`;
  }
  const dark = f.kind === "screen" || f.kind === "demo";
  const lines = f.visible_text.split("\n");
  const body = lines
    .map(
      (l, i) =>
        `<text x="120" y="${300 + i * 70}" font-size="40" fill="${dark ? "#e8e8ea" : "#1d1d1f"}" font-family="-apple-system, Helvetica, Arial, sans-serif">${esc(l)}</text>`,
    )
    .join("");
  const bars =
    f.kind === "slide" && /\d/.test(f.visible_text) && index % 2 === 1
      ? [0.35, 0.55, 0.8]
          .map((v, i) => `<rect x="${860 + i * 110}" y="${600 - v * 320}" width="70" height="${v * 320}" rx="6" fill="${accent}" opacity="${0.5 + i * 0.25}"/>`)
          .join("")
      : "";
  const board =
    f.kind === "whiteboard"
      ? `<rect width="${W}" height="${H}" fill="#fbfbf8"/><path d="M100 250 C 300 200, 500 300, 700 240" stroke="${accent}" stroke-width="5" fill="none"/>`
      : `<rect width="${W}" height="${H}" fill="${dark ? "#1c1c22" : bg}"/><rect x="0" y="0" width="${W}" height="14" fill="${accent}"/>`;
  const chrome = dark
    ? `<rect x="0" y="14" width="${W}" height="44" fill="#2a2a32"/><circle cx="36" cy="36" r="8" fill="#ff5f57"/><circle cx="62" cy="36" r="8" fill="#febc2e"/><circle cx="88" cy="36" r="8" fill="#28c840"/>`
    : "";
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${board}${chrome}
<text x="120" y="190" font-size="64" font-weight="700" fill="${dark ? "#fff" : accent}" font-family="-apple-system, Helvetica, Arial, sans-serif">${esc(f.title)}</text>
${body}${bars}
<text x="${W - 40}" y="${H - 30}" font-size="22" text-anchor="end" fill="${dark ? "#888" : "#8a8a8e"}" font-family="-apple-system, Helvetica, Arial, sans-serif">frame ${index + 1} · ${mm}:${ss}</text></svg>`;
}
