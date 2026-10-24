# Review studio and phone timeline keys

EX-Q1/EX-Q2: visible phone speaker keys and a readable chapter caption/timecode connect the meeting map to its real playback state. Previous/next chapter controls use the existing seek contract. A stronger meeting cover and coordinated frame, chapter, notes and transcript motion carry the studio identity through review.

Validation:45 frontend tests and production build passed. Motion helper tests cover live reduced motion, listener cleanup, repeated cancellation and unavailable/throwing API. Actual ready/paused/failed meetings and library were rendered at1440/390/320 using built-in safe API fixtures; no actual media ingestion or AI processing was run. Runtime keyboard, preference/interruption and chapter/transcript checks are recorded in external workspace evidence.

No CI workflow is configured in this repository. Review evidence remains local outside Git under output/design-expression-implementation-20261024/workspace-apps/evidence; no generated media is committed.
