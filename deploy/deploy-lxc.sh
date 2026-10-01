#!/usr/bin/env bash
# shellcheck disable=SC2029  # values are validated locally and meant to expand client-side
# Build and deploy Quill into its existing dedicated container.
#
#   deploy/deploy-lxc.sh [--allow-dirty] [--skip-tests] [--frontend-only] root@PROXMOX_HOST CTID
#
# Full deploy: backend tests + frontend build locally, release tarball from
# `git archive HEAD` plus frontend/dist, streamed into the CT, then
# deploy/install-release.sh (from that same tarball) runs inside the CT.
# --frontend-only: build frontend/dist and publish it into the current release
# without restarting anything.
# --allow-dirty: archive tracked working-tree changes (via `git stash create`);
# untracked files are never shipped.
#
# HARD RULE: nothing is ever written on the Proxmox host. Files travel as
#   ssh HOST "pct exec CTID -- sh -c 'cat > path'" < file
# and are unpacked inside the container. No scp, no `pct push`.
set -euo pipefail

allow_dirty=0; skip_tests=0; mode=full
while [ $# -gt 0 ]; do
  case "$1" in
    (--allow-dirty) allow_dirty=1; shift ;;
    (--skip-tests) skip_tests=1; shift ;;
    (--frontend-only) mode=frontend; shift ;;
    (-h|--help) sed -n '3,17p' "$0"; exit 0 ;;
    (--*) echo "Unknown option $1" >&2; exit 2 ;;
    (*) break ;;
  esac
done
HOST="${1:?Usage: deploy-lxc.sh [--allow-dirty] [--skip-tests] [--frontend-only] root@PROXMOX_HOST CTID}"
CTID="${2:?CTID required (the Quill container on that Proxmox host)}"
APP_URL="${APP_URL:-}"   # optional public check afterwards, e.g. https://quill.example.com
case "$CTID" in (*[!0-9]*|'') echo 'CTID must be numeric' >&2; exit 2;; esac
case "$HOST" in (*[!a-zA-Z0-9@._-]*) echo 'Invalid ssh target' >&2; exit 2;; esac

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT"
git rev-parse --is-inside-work-tree >/dev/null
prefix="$(git rev-parse --show-prefix)"   # empty when apps/quill is its own repo

paths=(.)
[ "$mode" = full ] || paths=(frontend deploy)
if [ -n "$(git status --porcelain --untracked-files=no -- "${paths[@]}")" ]; then
  if [ "$allow_dirty" != 1 ]; then
    git status --short --untracked-files=no -- "${paths[@]}" >&2
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

if [ "$mode" = full ] && [ "$skip_tests" != 1 ]; then
  echo '--- backend tests'
  py="${PYTHON:-python3}"
  [ -n "${PYTHON:-}" ] || [ ! -x backend/.venv/bin/python ] || py="$APP_ROOT/backend/.venv/bin/python"
  (cd backend && QUILL_DIARIZER_FAKE=1 "$py" -m pytest -q) \
    || { echo 'Backend tests failed; not deploying' >&2; exit 1; }
fi

echo '--- frontend build'
(cd frontend && npm ci && npm run build)
test -f frontend/dist/index.html

work="$(mktemp -d "${TMPDIR:-/tmp}/quill-release.XXXXXX")"
trap 'rm -rf "$work"' EXIT
stage="$work/stage"; mkdir -p "$stage"
if [ "$mode" = full ]; then
  git archive --format=tar "$ref:$prefix" | tar -xf - -C "$stage"
  rm -rf "$stage/frontend/dist"
  mkdir -p "$stage/frontend"
else
  mkdir -p "$stage/frontend" "$stage/deploy"
  git archive --format=tar "$ref:${prefix}deploy" install-release.sh | tar -xf - -C "$stage/deploy"
fi
cp -R frontend/dist "$stage/frontend/dist"
printf 'commit=%s dirty=%s built=%s by=%s\n' "$sha" "$dirty" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(whoami)" > "$stage/RELEASE"
archive="$work/quill.tar.gz"
# COPYFILE_DISABLE keeps macOS ._ AppleDouble files out of the tarball.
(cd "$stage" && COPYFILE_DISABLE=1 tar --no-xattrs -czf "$archive" -- *)
remote="/tmp/quill-$mode-$sha-$$.tar.gz"
echo "--- streaming $(du -h "$archive" | cut -f1) $mode release $sha (dirty=$dirty) into CT $CTID"

# Stream straight into the container: nothing is ever written on the Proxmox host.
ssh "$HOST" "pct exec $CTID -- sh -c 'umask 077; cat > $remote'" < "$archive"

# Run the installer shipped inside the archive itself, so script and release match.
flag=
[ "$mode" = full ] || flag=--frontend-only
ssh "$HOST" "pct exec $CTID -- bash -s -- '$remote' '$flag'" <<'REMOTE'
set -euo pipefail
archive="$1"; flag="$2"
work="$(mktemp -d /tmp/quill-installer.XXXXXX)"
trap 'rm -rf "$work"' EXIT
tar --no-same-owner -xzf "$archive" -C "$work" deploy/install-release.sh
if [ -n "$flag" ]; then
  bash "$work/deploy/install-release.sh" "$flag" "$archive" </dev/null
else
  bash "$work/deploy/install-release.sh" "$archive" </dev/null
fi
REMOTE

if [ -n "$APP_URL" ]; then
  curl -fsS --max-time 15 -o /dev/null "$APP_URL/api/auth/state"
  curl -fsS --max-time 15 -o /dev/null "$APP_URL/"
  echo "Public checks passed for $APP_URL"
fi
echo "Deployed quill $sha ($mode) to CT $CTID on $HOST"
