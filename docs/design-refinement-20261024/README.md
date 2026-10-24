# Paused processing refinement

The paused-stage banner leads with the task and a concise reason for known model diagnostics. Exact diagnostic text remains in Technical details, with Models still restricted to administrators. Unknown reasons use a neutral summary. The worker retries paused stages (`backend/quill/worker.py`); the banner makes no storage guarantee.

The pipeline, actual progress, media player and recovery permissions are preserved. Paused progress stays still; this change adds no animation.

Validation: 43 frontend tests and production build; 250 backend tests passed. Current applied Library and paused-meeting fixtures rendered at 1440, 390 and 320 pixels, with keyboard access and reduced-motion inspection. Data was synthetic; no live recordings, model recovery or storage guarantee was tested.

Images and runtime receipts are retained locally outside Git under `output/design-refinement-20261024/workspace-apps/` in the review workspace. No review images or video are committed.
