# Processing recovery verification · 24 October 2026

The existing meeting layout, stage cards, palette and controls remain. A stopped diarization stage now says **Speaker detection stopped**, explains the next action and offers **Retry speaker detection**. Exact diagnostics remain available in a native Technical details disclosure. Library cards use task wording and point to details. No cause is inferred from process exit status 137, and no unsupported original-retention promise was added. Retry still uses the existing stage key and owner permissions.

## Evidence



## Verification

- 7 frontend files / 41 tests passed; TypeScript and production build passed.
- Added regression verifies task wording, retained raw diagnostics, no inferred memory diagnosis and no retry for shared viewers.
- At all three widths the failure layout has no page overflow, readable text, reachable retry and keyboard-operable Technical details that opens and closes with Enter.
- Clicking retry sends the unchanged `{stage: "diarize", options: {}}` and clears the mocked failure after acknowledgment.
- Existing reduced-motion CSS suppresses the page entrance to 0.001 ms; no pre-request motion was added.

Live processing, permissions against a production account and physical devices were outside this local verification.

Visual evidence is retained locally outside Git. Review images and media are not committed; these notes retain the text verification and fixture boundaries.
