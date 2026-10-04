# Using Quill

Quill turns an existing meeting recording into a reader with speaker-attributed
text, key slides and timestamped notes. It is built by Team AER for a small team
running its own installation. Start with the [README](../README.md) for setup and
the [deployment guide](../deploy/README.md) for a production install.

## Upload a recording

Sign in, then drag audio or video into the app or use the upload button. Review the
suggested title before starting. The upload sheet offers automatic language
detection or a language hint, an optional expected speaker count from one to eight,
and **Audio only** for videos whose pictures are not useful. Language support and
accuracy depend on the speech model configured by your administrator.

The browser uses resumable tus uploads in 64 MiB chunks. Its file limit is 10 GiB;
the server can set a lower limit with `QUILL_MAX_UPLOAD_BYTES`. Resuming still needs
the original local file. Progress appears in the library while you continue using
the app.

The worker probes the recording, extracts audio, identifies speaker turns and
submits transcription jobs. Video mode then selects key moments and reads frames;
both modes finish by generating notes. Stages checkpoint their work. A disabled
gateway model pauses processing; other failures can be retried from the reader.

## Read and review

- **Player and meeting map:** seek from a chapter, speaker lane, frame or transcript
  line. The player and transcript share the current timestamp.
- **Transcript:** follow playback, search within the meeting, filter by speaker and
  copy a quote or link to a particular moment.
- **Speakers:** rename detected speakers or merge duplicates with owner/edit access.
  Speaker labels are estimates, not verified identities. The owner can rerun detection.
- **Frames:** review extracted slides and screens with their model-generated text.
  Audio mode skips these results.
- **Notes:** read the TL;DR, summary, chapters, decisions, open questions and action
  items. Use timestamp links to check grounded items against the recording.
- **Action items:** review suggested owners and due dates, then copy the checklist.
  Checked items are stored in the browser's local preferences; they are not a shared
  team task tracker.

Use **⌘K / Ctrl+K** for the command palette and the app's shortcut help for other
reader controls. Global search finds transcript and frame text across meetings
you can access. Notes can be copied as Markdown.

Generated content may be incomplete or wrong. Verify decisions, names, owners and
dates against the recording before using them in a team update.

## Share and export

The owner can share a meeting with individual signed-in teammates or everyone on
the installation, with **view** or **edit** access. Links to moments use the same
access checks; copying a link does not make the meeting public. Owners control
sharing, deletion and reruns. See [accounts and sharing](ACCOUNTS.md) for roles,
invitations, sessions and account recovery.

The download menu supports:

| Format | Contents and use |
| --- | --- |
| Markdown | Notes, transcript and frame text for a readable document |
| TXT | Timestamped transcript with speaker names |
| SRT / VTT | Transcript subtitles with speaker attribution |
| JSON | Structured meeting export for other tools |
| RTTM | Speaker turns for diarization tools |

Subtitles use transcript-segment timings; do not assume word-level alignment.
An export is a separate copy and is not removed when a meeting is deleted in Quill.

## Models, storage and retention

Quill stores uploads, SQLite data and derived media under `QUILL_DATA_DIR`.
Audio clips, selected frames and text go to your configured gateway for inference.
The app/diarizer use CPU; the remote speech, vision and text services have their own
hardware requirements. Quill needs a durable STT jobs API, a model catalog and chat
completions with structured output, including image input for frame analysis.

Admins can select model IDs and video retention in Settings. Source video is kept
for 14 days by default after processing; `0` deletes it after processing and a
negative value retains it. This setting does not delete all derived files or notes.
Keep backups outside the data volume if you need recovery after storage failure;
the [deployment guide](../deploy/README.md) explains the SQLite backup arrangement.

The diarizer has eight speaker slots and can merge similar voices in larger groups.
Audio-only meetings have no frame results. Quill processes recordings rather than
joining calls, and the frontend's mock mode supplies sample data rather than running
real model jobs. The [design plan](PLAN.md) is historical context; source code and
current runtime settings determine installed behavior.
