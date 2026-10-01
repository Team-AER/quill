#!/usr/bin/env python3
"""Online SQLite snapshot of the Quill database (backup API), gzip-compressed.

    backup.py [--label nightly] [--keep 7]

Writes {DATA_DIR}/backups/quill-<label>-<UTC stamp>.sqlite3.gz, verifies the
snapshot with PRAGMA integrity_check before publishing it, and keeps the newest
--keep archives for that label. Prints the archive path as the only stdout line
(install-release.sh uses it for pre-deploy snapshots). Media files are not
included: they are large, re-derivable or user-owned originals.
Exit 3 means there is no database yet (nothing to back up).
"""
import argparse
import datetime
import gzip
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

os.umask(0o077)
parser = argparse.ArgumentParser()
parser.add_argument("--label", default="nightly")
parser.add_argument("--keep", type=int, default=7)
args = parser.parse_args()
if not re.fullmatch(r"[a-z0-9-]+", args.label) or args.keep < 1:
    parser.error("label must be [a-z0-9-]+ and keep >= 1")

data = Path(os.getenv("QUILL_DATA_DIR", "/var/lib/quill"))
database = data / "db" / "quill.sqlite3"
backups = data / "backups"
backups.mkdir(parents=True, exist_ok=True)
if not database.exists():
    print(f"No Quill database at {database}; nothing to back up", file=sys.stderr)
    raise SystemExit(3)

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
target = backups / f"quill-{args.label}-{stamp}.sqlite3.gz"
partial = target.with_name(target.name + ".partial")
with tempfile.TemporaryDirectory(prefix="quill-backup-", dir=backups) as work:
    snapshot = Path(work) / database.name
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(snapshot)
        try:
            source.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SystemExit("Snapshot integrity check failed")
        finally:
            dest.close()
    finally:
        source.close()
    with open(snapshot, "rb") as raw, gzip.open(partial, "wb", compresslevel=6) as packed:
        shutil.copyfileobj(raw, packed, 1024 * 1024)
    partial.replace(target)

for old in sorted(backups.glob(f"quill-{args.label}-*.sqlite3.gz"), reverse=True)[args.keep:]:
    old.unlink()
print(f"Saved {target.name} ({target.stat().st_size} bytes)", file=sys.stderr)
print(target)
