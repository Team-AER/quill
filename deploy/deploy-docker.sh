#!/usr/bin/env bash
# shellcheck disable=SC2029  # values are validated locally and meant to expand client-side
# Build and deploy Quill as Docker containers inside its dedicated LXC.
#
#   deploy/deploy-docker.sh [--allow-dirty] [--skip-tests] root@PROXMOX_HOST CTID
#
# Ships `git archive HEAD` (plus a RELEASE stamp) into the CT, installs Docker
# Engine there if it is missing (deploy/install-docker.sh), then runs
# deploy/docker-release.sh from that same archive: the images are built inside
# the CT, so the release always matches the script that installs it.
# --allow-dirty: archive tracked working-tree changes (via `git stash create`);
# untracked files are never shipped.
#
# HARD RULE: nothing is ever written on the Proxmox host. Files travel as
#   ssh HOST "pct exec CTID -- sh -c 'cat > path'" < file
# and are unpacked inside the container. No scp, no `pct push`.
set -euo pipefail

allow_dirty=0; skip_tests=0
while [ $# -gt 0 ]; do
  case "$1" in
    (--allow-dirty) allow_dirty=1; shift ;;
    (--skip-tests) skip_tests=1; shift ;;
    (-h|--help) sed -n '3,17p' "$0"; exit 0 ;;
    (--*) echo "Unknown option $1" >&2; exit 2 ;;
    (*) break ;;
  esac
done
HOST="${1:?Usage: deploy-docker.sh [--allow-dirty] [--skip-tests] root@PROXMOX_HOST CTID}"
CTID="${2:?CTID required (the Quill container on that Proxmox host)}"
APP_URL="${APP_URL:-}"   # optional public check afterwards, e.g. https://quill.example.com
case "$CTID" in (*[!0-9]*|'') echo 'CTID must be numeric' >&2; exit 2;; esac
case "$HOST" in (*[!a-zA-Z0-9@._-]*) echo 'Invalid ssh target' >&2; exit 2;; esac

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT"
git rev-parse --is-inside-work-tree >/dev/null
prefix="$(git rev-parse --show-prefix)"   # empty when apps/quill is its own repo

if [ -n "$(git status --porcelain --untracked-files=no -- .)" ]; then
  if [ "$allow_dirty" != 1 ]; then
    git status --short --untracked-files=no -- . >&2
    echo 'Working tree has uncommitted changes; commit them or pass --allow-dirty' >&2
    exit 1
  fi
  ref="$(git stash create)"; ref="${ref:-HEAD}"; dirty=1
else
  ref=HEAD; dirty=0
fi
sha="$(git rev-parse --short HEAD)"

# Refuse early if the target is not the Quill container (read-only on the host).
identity="$(ssh "$HOST" "pct config $CTID" | awk '/^hostname: / {print $2}')"
test "$identity" = quill || { echo "CT $CTID on $HOST is not named quill (got '$identity')" >&2; exit 1; }
ssh "$HOST" "pct status $CTID" | grep -q 'status: running' || { echo "CT $CTID is not running" >&2; exit 1; }

if [ "$skip_tests" != 1 ]; then
  echo '--- backend tests'
  py="${PYTHON:-python3}"
  [ -n "${PYTHON:-}" ] || [ ! -x backend/.venv/bin/python ] || py="$APP_ROOT/backend/.venv/bin/python"
  (cd backend && QUILL_DIARIZER_FAKE=1 "$py" -m pytest -q) \
    || { echo 'Backend tests failed; not deploying' >&2; exit 1; }
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/quill-docker.XXXXXX")"
trap 'rm -rf "$work"' EXIT
stage="$work/stage"; mkdir -p "$stage"
git archive --format=tar "$ref:$prefix" | tar -xf - -C "$stage"
printf 'commit=%s dirty=%s built=%s by=%s\n' "$sha" "$dirty" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(whoami)" > "$stage/RELEASE"
archive="$work/quill-src.tar.gz"
# COPYFILE_DISABLE keeps macOS ._ AppleDouble files out of the tarball.
(cd "$stage" && COPYFILE_DISABLE=1 tar --no-xattrs -czf "$archive" -- .)
remote="/tmp/quill-src-$sha-$$.tar.gz"
echo "--- streaming $(du -h "$archive" | cut -f1) source $sha (dirty=$dirty) into CT $CTID"

# Stream straight into the container: nothing is ever written on the Proxmox host.
ssh "$HOST" "pct exec $CTID -- sh -c 'umask 077; cat > $remote'" < "$archive"

ssh "$HOST" "pct exec $CTID -- bash -s -- '$remote'" <<'REMOTE'
set -euo pipefail
archive="$1"
work="$(mktemp -d /tmp/quill-installer.XXXXXX)"
trap 'rm -rf "$work"' EXIT
tar --no-same-owner -xzf "$archive" -C "$work" ./deploy/docker-release.sh ./deploy/install-docker.sh
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  bash "$work/deploy/install-docker.sh" </dev/null
fi
bash "$work/deploy/docker-release.sh" "$archive" </dev/null
REMOTE

if [ -n "$APP_URL" ]; then
  curl -fsS --max-time 15 -o /dev/null "$APP_URL/api/auth/state"
  curl -fsS --max-time 15 -o /dev/null "$APP_URL/"
  echo "Public checks passed for $APP_URL"
fi
echo "Deployed quill $sha (docker) to CT $CTID on $HOST"
