# Quill web design

Quill follows the AER glass language shared with Hedwig: system font stack only, glass for the
rail and overlays, opaque sheets for content, one blue accent, orange only for attention
(paused), red only for failures. Speakers own the colour: the eight `--spk-*` hues appear on
avatars, timeline lanes, talk-time bars and transcript names.

## Surfaces

- **Library**: date groups (Today, Yesterday, Earlier this week, then months), grid or list
  (remembered per browser). Cards show a cover (best key frame from the API's `cover_url`, or
  speaker-coloured speech bars for audio), duration, speaker avatars, the first TL;DR line
  (`gist`), and a processing overlay with a progress ring and per-stage pips.
- **Uploads**: drop files anywhere, press U, or use New meeting. The upload sheet cleans titles
  from file names (`lib/title.ts`) and sets language, speakers and audio-only for the batch.
- **Meeting**: custom player controls (chapter ticks on the scrubber, speed, mute, PiP,
  full screen, resume from the last position), a meeting map (chapters, one lane per speaker,
  key-frame pins, hover preview with frame, chapter and who is speaking), Overview with TL;DR,
  talk time and chapter thumbnails, tickable action items (kept per browser), and a transcript
  with avatars, speaker filter chips, follow mode and copy quote / copy link per line.
- **⌘K** jumps to meetings, searches transcripts and runs actions; **?** lists shortcuts.
- **Accounts** (docs/ACCOUNTS.md): Settings is Account for everyone (profile, password,
  devices, appearance) plus People and System for admins. Invites and password resets are
  one-time links shown once in a share panel (copy link, copy a ready-to-send message, share).
  Invite and reset links open their own glass card that says who invited you, which email
  you'll use, and why a dead link can't be used. New passwords get show/hide and a strength
  hint instead of a confirm field.
- **Sharing**: owners get a Share button on the meeting header (sheet: add people, Can view /
  Can edit, everyone on this Quill, copy link). Shared meetings carry the owner's name on the
  cover and a "Name · Can view" chip on the meeting header. The Library gains a "Shared with
  me" filter, and controls that need edit or owner access are hidden rather than disabled.
  Anyone without a display name sees a slim prompt above the page.

## Motion

Pages rise in, cards stagger, progress bars stripe while a stage runs, the live dot pulses and
the playing transcript line shows a small equaliser. Everything collapses under
`prefers-reduced-motion`.
