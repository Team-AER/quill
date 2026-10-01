#!/usr/bin/env bash
# Run as root INSIDE the dedicated Quill container (deploy-lxc.sh streams the
# release in and runs this via `pct exec`). Nothing here touches the Proxmox host.
#
#   install-release.sh /tmp/quill-release.tar.gz                 full release
#   install-release.sh --frontend-only /tmp/quill-frontend.tar.gz  publish dist only
#
# Full release:
#   1. unpack to /opt/quill/releases/<ts>, verify layout
#   2. backend venv  /opt/quill/venvs/backend-<hash of pyproject deps>   -> /opt/quill/venv
#      diarizer venv /opt/quill/venvs/diarizer-<hash of lock>            -> /opt/quill/diarizer-venv
#      (built only when the hash is new, while the old release keeps running)
#   3. import preflight, /etc/quill/quill.env on first install, units + nginx site
#   4. stop worker (stages resume on restart) and api, pre-deploy SQLite snapshot
#   5. switch symlinks, `python -m quill.db migrate`, start api + worker
#   6. health: GET /api/auth/state direct and through nginx, worker stays up
#   7. on any failure after the switch: roll back code, venvs and database
#   8. keep the newest 3 releases and the venvs they reference
set -euo pipefail
export LC_ALL=C

mode=full
if [ "${1:-}" = --frontend-only ]; then mode=frontend; shift; fi
archive="${1:?Usage: install-release.sh [--frontend-only] ARCHIVE.tar.gz}"
test "$(id -u)" = 0 || { echo 'install-release.sh must run as root inside the CT' >&2; exit 1; }
test -f "$archive" || { echo "No archive at $archive" >&2; exit 1; }
test "$(hostname)" = quill || { echo "Refusing: this host is '$(hostname)', not quill" >&2; exit 1; }

exec 9>/run/lock/quill-deploy.lock
flock -n 9 || { echo 'Another Quill deployment is running' >&2; exit 1; }

ROOT=/opt/quill
DATA=/var/lib/quill
DB="$DATA/db/quill.sqlite3"
ENV_FILE=/etc/quill/quill.env
API=http://127.0.0.1:8020
KEEP_RELEASES=3
PYTORCH_CPU_INDEX=https://download.pytorch.org/whl/cpu
export PIP_CACHE_DIR=/var/cache/quill-pip PIP_DISABLE_PIP_VERSION_CHECK=1

log() { printf '[quill-install] %s\n' "$*" >&2; }

health() {  # health URL [attempts]
  local url="$1" attempts="${2:-45}"
  for _ in $(seq 1 "$attempts"); do
    if curl -fsS --max-time 3 -o /dev/null "$url"; then return 0; fi
    sleep 1
  done
  return 1
}

# Publish dist files into a live release: assets first, index.html last, and old
# hashed assets are kept so already-open tabs can still lazy-load their chunks.
publish_frontend() {  # publish_frontend STAGED_DIST DEST_DIST
  python3 - "$1" "$2" <<'PY'
from pathlib import Path
import os, re, shutil, sys
stage, destination = map(Path, sys.argv[1:])
index = stage / 'index.html'
assert index.is_file(), 'Built frontend has no index.html'
assets = re.findall(r'(?:src|href)=["\'](/assets/[^"\']+)["\']', index.read_text())
assert assets, 'Built index.html references no /assets/ bundles'
for asset in assets:
    target = (stage / asset.lstrip('/')).resolve()
    assert target.is_relative_to(stage.resolve()) and target.is_file(), f'Missing built asset {asset}'
assert not any(p.is_symlink() for p in stage.rglob('*')), 'Frontend bundle contains a symlink'
def publish(source):
    target = destination / source.relative_to(stage)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.deploy-next')
    shutil.copyfile(source, temporary)
    temporary.chmod(0o644)
    os.replace(temporary, target)
for source in sorted(stage.rglob('*')):
    if source.is_file() and source != index:
        publish(source)
publish(index)
print(f'Published {len(assets)} entry bundle(s)')
PY
}

# ---------------------------------------------------------------------------
if [ "$mode" = frontend ]; then
  release="$(readlink -f "$ROOT/current" || true)"
  test -n "$release" && test -d "$release/frontend/dist" || { echo 'No current release; run a full deploy first' >&2; exit 1; }
  stage="$(mktemp -d /tmp/quill-frontend.XXXXXX)"
  trap 'rm -rf "$stage"; rm -f "$archive"' EXIT
  tar --no-same-owner -xzf "$archive" -C "$stage"
  test -f "$stage/frontend/dist/index.html" || { echo 'Archive has no frontend/dist/index.html' >&2; exit 1; }
  api_before="$(systemctl show quill-api -p MainPID --value)"
  publish_frontend "$stage/frontend/dist" "$release/frontend/dist"
  chmod -R a+rX "$release/frontend/dist"
  test "$(systemctl show quill-api -p MainPID --value)" = "$api_before"
  health http://127.0.0.1/ 5 || { echo 'nginx does not serve / after frontend publish' >&2; exit 1; }
  curl -fsS --max-time 5 http://127.0.0.1/index.html | cmp -s - "$stage/frontend/dist/index.html" \
    || { echo 'Served index.html does not match the published one' >&2; exit 1; }
  printf 'Quill frontend updated in %s (api process unchanged)\n' "$release"
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. unpack
id quill >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin quill
install -d -m 0755 "$ROOT" "$ROOT/releases" "$ROOT/venvs"
install -d -m 0750 -g quill /etc/quill
install -d -o quill -g quill -m 0750 "$DATA"
for d in db uploads media backups cache; do install -d -o quill -g quill -m 0700 "$DATA/$d"; done
install -d -m 0700 "$PIP_CACHE_DIR"

release="$ROOT/releases/$(date -u +%Y%m%dT%H%M%SZ)"
test ! -e "$release" || release="$release-$$"
install -d -m 0755 "$release"
tar --no-same-owner -xzf "$archive" -C "$release"
rm -f "$archive"
# Bundles can come from shells with a private umask; code holds no secrets.
chmod -R a+rX,go-w "$release"
for required in backend/pyproject.toml backend/quill/__init__.py frontend/dist/index.html \
                deploy/quill-api.service deploy/quill-worker.service deploy/nginx-quill.conf \
                deploy/backup.py deploy/quill.env.example; do
  test -f "$release/$required" || { echo "Release is missing $required" >&2; exit 1; }
done
diarizer_lock=
for candidate in requirements-lock-linux-x86_64-cpu.txt requirements.lock requirements-lock.txt requirements.txt; do
  if [ -f "$release/diarizer/$candidate" ]; then diarizer_lock="$release/diarizer/$candidate"; break; fi
done
test -n "$diarizer_lock" || { echo 'Release has no diarizer/requirements.lock' >&2; exit 1; }
log "unpacked $(cat "$release/RELEASE" 2>/dev/null | tr '\n' ' ') into $release"

# 2. virtualenvs (content-addressed; the running release is untouched)
python3 - "$release/backend/pyproject.toml" > "$release/.backend-requirements.txt" <<'PY'
import sys, tomllib
with open(sys.argv[1], 'rb') as handle:
    deps = tomllib.load(handle).get('project', {}).get('dependencies', [])
if not deps:
    raise SystemExit('backend/pyproject.toml has no [project].dependencies')
print('\n'.join(sorted(deps)))
PY
py_version="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
backend_hash="$( { echo "$py_version"; cat "$release/.backend-requirements.txt"; } | sha256sum | cut -c1-16)"
diarizer_hash="$( { echo "$py_version"; echo "$PYTORCH_CPU_INDEX"; cat "$diarizer_lock"; } | sha256sum | cut -c1-16)"
backend_venv="$ROOT/venvs/backend-$backend_hash"
diarizer_venv="$ROOT/venvs/diarizer-$diarizer_hash"

if [ ! -f "$backend_venv/.complete" ]; then
  log "building backend venv $backend_venv"
  rm -rf "$backend_venv"   # venvs are not relocatable; build in place, mark when done
  python3 -m venv "$backend_venv"
  "$backend_venv/bin/pip" install -q --upgrade pip wheel
  "$backend_venv/bin/pip" install -r "$release/.backend-requirements.txt"
  touch "$backend_venv/.complete"
else
  log "reusing backend venv $backend_venv"
fi
test -x "$backend_venv/bin/uvicorn" || { echo 'backend dependencies do not provide uvicorn' >&2; exit 1; }

if [ ! -f "$diarizer_venv/.complete" ]; then
  log "building diarizer venv $diarizer_venv from $(basename "$diarizer_lock") (torch CPU wheels)"
  rm -rf "$diarizer_venv"
  python3 -m venv "$diarizer_venv"
  "$diarizer_venv/bin/pip" install -q --upgrade pip wheel
  "$diarizer_venv/bin/pip" install --index-url https://pypi.org/simple \
    --extra-index-url "$PYTORCH_CPU_INDEX" -r "$diarizer_lock"
  touch "$diarizer_venv/.complete"
else
  log "reusing diarizer venv $diarizer_venv (lock unchanged)"
fi
printf '%s\n' "$backend_venv" > "$release/.venv-backend"
printf '%s\n' "$diarizer_venv" > "$release/.venv-diarizer"

# Nemotron-3-Diarization weights (~400 MB) live on the data disk and survive releases;
# the diarizer runs with HF_HUB_OFFLINE=1 so a job never waits on a download.
DIARIZER_MODEL_DIR="$DATA/models/nemotron-3-diarization"
if [ ! -s "$DIARIZER_MODEL_DIR/model.safetensors" ]; then
  log "downloading nvidia/Nemotron-3-Diarization into $DIARIZER_MODEL_DIR"
  install -d -o quill -g quill -m 0750 "$DATA/models" "$DIARIZER_MODEL_DIR"
  HF_HOME="$DATA/cache/huggingface" "$diarizer_venv/bin/python" - "$DIARIZER_MODEL_DIR" <<'PY'
import sys
from huggingface_hub import snapshot_download
snapshot_download("nvidia/Nemotron-3-Diarization", local_dir=sys.argv[1],
                  allow_patterns=["config.json", "processor_config.json", "model.safetensors"])
PY
  chown -R quill:quill "$DATA/models" "$DATA/cache"
fi

# 3. preflight the new code with its own venv before stopping anything
log 'import preflight'
systemd-run --quiet --wait --pipe --collect -p User=quill -p Group=quill -p PrivateTmp=yes \
  -p WorkingDirectory="$release/backend" \
  -E PYTHONPATH="$release/backend" -E PYTHONDONTWRITEBYTECODE=1 -E HOME=/tmp \
  -E QUILL_DATA_DIR=/tmp/quill-preflight -E QUILL_SECRET_KEY=preflight-only \
  "$backend_venv/bin/python" -c 'import quill.app, quill.worker, quill.db; print("import ok")' </dev/null

if [ ! -f "$ENV_FILE" ]; then
  log "creating $ENV_FILE with a fresh QUILL_SECRET_KEY"
  secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  umask 077
  sed "s|^QUILL_SECRET_KEY=.*|QUILL_SECRET_KEY=$secret|" "$release/deploy/quill.env.example" > "$ENV_FILE.new"
  umask 022
  chown root:quill "$ENV_FILE.new"; chmod 0640 "$ENV_FILE.new"
  mv -f "$ENV_FILE.new" "$ENV_FILE"
fi
grep -Eq '^QUILL_SECRET_KEY=.{32,}' "$ENV_FILE" || { echo "$ENV_FILE has no QUILL_SECRET_KEY" >&2; exit 1; }
# Per-install nginx trust for X-Forwarded-For (included by nginx-quill.conf).
if [ ! -f /etc/quill/nginx-real-ip.conf ]; then
  log 'creating /etc/quill/nginx-real-ip.conf (commented; set your reverse proxy address)'
  cat > /etc/quill/nginx-real-ip.conf <<'NGINX'
# Trust X-Forwarded-For only from your TLS reverse proxy, so Quill sees real client IPs.
# Uncomment and set its address, then re-run a deploy (or `nginx -t && systemctl reload nginx`).
# set_real_ip_from 192.168.1.2;
# real_ip_header X-Forwarded-For;
# real_ip_recursive on;
NGINX
  chmod 0644 /etc/quill/nginx-real-ip.conf
fi
# Report (never auto-add) settings introduced by newer releases.
missing="$(grep -Eo '^QUILL_[A-Z0-9_]+' "$release/deploy/quill.env.example" | while read -r key; do
  grep -q "^$key=" "$ENV_FILE" || printf '%s ' "$key"; done)"
[ -z "$missing" ] || log "note: $ENV_FILE lacks $missing(defaults apply; compare with deploy/quill.env.example)"

previous="$(readlink -f "$ROOT/current" || true)"
prev_backend_venv="$(readlink "$ROOT/venv" || true)"
prev_diarizer_venv="$(readlink "$ROOT/diarizer-venv" || true)"
if [ -n "$previous" ] && [ -d "$previous/frontend/dist/assets" ]; then
  # Open browser tabs may still request the previous hashed bundles.
  cp -an "$previous/frontend/dist/assets/." "$release/frontend/dist/assets/" 2>/dev/null || true
fi

for unit in quill-api.service quill-worker.service quill-backup.service quill-backup.timer; do
  install -m 0644 "$release/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
[ ! -f "$release/deploy/quill-users" ] || install -m 0755 "$release/deploy/quill-users" /usr/local/bin/quill-users
nginx_backup="$(mktemp /tmp/quill-nginx.XXXXXX)"
if [ -f /etc/nginx/sites-available/quill ]; then cp -p /etc/nginx/sites-available/quill "$nginx_backup"; else : > "$nginx_backup"; fi
install -m 0644 "$release/deploy/nginx-quill.conf" /etc/nginx/sites-available/quill
ln -sfn /etc/nginx/sites-available/quill /etc/nginx/sites-enabled/quill
rm -f /etc/nginx/sites-enabled/default
if ! nginx -t 2>/dev/null; then
  nginx -t || true
  if [ -s "$nginx_backup" ]; then cp -p "$nginx_backup" /etc/nginx/sites-available/quill; fi
  echo 'New nginx site fails nginx -t; previous site restored, services untouched' >&2
  exit 1
fi

# 4. stop, snapshot
link() { ln -sfn "$2" "$1.next"; mv -Tf "$1.next" "$1"; }
reinstall_diarizer() {  # reinstall_diarizer VENV RELEASE_DIR  (package code only, deps untouched)
  if [ -f "$2/diarizer/pyproject.toml" ] || [ -f "$2/diarizer/setup.py" ]; then
    "$1/bin/pip" install -q --no-deps --force-reinstall "$2/diarizer"
  fi
}

log 'stopping worker (in-flight stage resumes on restart) and api'
systemctl stop quill-worker.service || true
systemctl stop quill-api.service || true

snapshot=
if [ -f "$DB" ]; then
  if ! snapshot="$(runuser -u quill -- env QUILL_DATA_DIR="$DATA" python3 "$release/deploy/backup.py" --label pre-deploy --keep 5)"; then
    log 'pre-deploy backup failed; restarting the previous release unchanged'
    if [ -n "$previous" ]; then systemctl start quill-api.service quill-worker.service; fi
    exit 1
  fi
  log "pre-deploy snapshot $snapshot"
fi

rollback() {
  trap - ERR
  set +e
  log "ROLLBACK: $1"
  systemctl stop quill-worker.service quill-api.service
  journalctl -u quill-api -u quill-worker -n 40 --no-pager
  if [ -n "$snapshot" ] && [ "${migrated:-0}" = 1 ]; then
    log "restoring database from $snapshot"
    rm -f "$DB-wal" "$DB-shm"
    runuser -u quill -- python3 -c 'import gzip, shutil, sys
with gzip.open(sys.argv[1], "rb") as src, open(sys.argv[2] + ".restore", "wb") as dst:
    shutil.copyfileobj(src, dst, 1 << 20)' "$snapshot" "$DB" && mv -f "$DB.restore" "$DB"
  fi
  if [ -n "$previous" ] && [ -d "$previous" ]; then
    [ -z "$prev_backend_venv" ] || link "$ROOT/venv" "$prev_backend_venv"
    [ -z "$prev_diarizer_venv" ] || { link "$ROOT/diarizer-venv" "$prev_diarizer_venv"; reinstall_diarizer "$prev_diarizer_venv" "$previous"; }
    link "$ROOT/current" "$previous"
    for unit in quill-api.service quill-worker.service quill-backup.service quill-backup.timer; do
      [ -f "$previous/deploy/$unit" ] && install -m 0644 "$previous/deploy/$unit" "/etc/systemd/system/$unit"
    done
    systemctl daemon-reload
    if [ -s "$nginx_backup" ]; then cp -p "$nginx_backup" /etc/nginx/sites-available/quill && nginx -t && systemctl reload nginx; fi
    systemctl start quill-api.service quill-worker.service
    if health "$API/api/auth/state" 30; then
      log "rolled back to $previous (healthy); failed release kept at $release for diagnosis"
    else
      log "rolled back to $previous but it is NOT healthy either - investigate now"
    fi
  else
    log "first install failed; nothing to roll back to. Release kept at $release"
  fi
  rm -f "$nginx_backup"
  exit 1
}

# 5. switch + migrate + start
migrated=0
# Any unexpected failure from here until the release is healthy rolls back.
trap 'rollback "unexpected error at line $LINENO"' ERR
reinstall_diarizer "$diarizer_venv" "$release" || rollback 'diarizer package install failed'
test -x "$diarizer_venv/bin/quill-diarize" || rollback 'diarizer venv has no quill-diarize entry point'
link "$ROOT/venv" "$backend_venv"
link "$ROOT/diarizer-venv" "$diarizer_venv"
link "$ROOT/current" "$release"
[ -z "$previous" ] || link "$ROOT/previous" "$previous"

log 'running database migrations (python -m quill.db migrate)'
migrated=1
systemd-run --quiet --wait --pipe --collect -p User=quill -p Group=quill \
  -p EnvironmentFile="$ENV_FILE" -p WorkingDirectory="$ROOT/current/backend" \
  -E PYTHONPATH="$ROOT/current/backend" -E PYTHONDONTWRITEBYTECODE=1 -E HOME="$DATA" \
  "$ROOT/venv/bin/python" -m quill.db migrate </dev/null || rollback 'migration failed'

systemctl enable --quiet quill-api.service quill-worker.service
systemctl enable --quiet --now quill-backup.timer
systemctl reload nginx || rollback 'nginx reload failed'
systemctl start quill-api.service || rollback 'quill-api failed to start'

# 6. health
health "$API/api/auth/state" 45 || rollback "API did not answer GET /api/auth/state"
health http://127.0.0.1/api/auth/state 10 || rollback 'nginx does not proxy /api/auth/state'
curl -fsS --max-time 5 -o /dev/null http://127.0.0.1/ || rollback 'nginx does not serve the frontend'
systemctl start quill-worker.service || rollback 'quill-worker failed to start'
sleep 8
systemctl is-active --quiet quill-worker.service || rollback 'quill-worker exited shortly after start'
[ "$(systemctl show quill-worker -p NRestarts --value)" = 0 ] || rollback 'quill-worker is crash-looping'
trap - ERR
rm -f "$nginx_backup"

# 8. retention: newest releases plus whatever current/previous point at; venvs they use
current="$(readlink -f "$ROOT/current")"
prev_keep="$(readlink -f "$ROOT/previous" || true)"
find "$ROOT/releases" -mindepth 1 -maxdepth 1 -type d | sort -r | tail -n +$((KEEP_RELEASES + 1)) |
  while read -r old; do
    [ "$old" = "$current" ] || [ "$old" = "$prev_keep" ] || { log "pruning release $old"; rm -rf "$old"; }
  done
keep_venvs="$(cat "$ROOT"/releases/*/.venv-backend "$ROOT"/releases/*/.venv-diarizer 2>/dev/null | sort -u)"
keep_venvs="$keep_venvs
$(readlink "$ROOT/venv" || true)
$(readlink "$ROOT/diarizer-venv" || true)"
find "$ROOT/venvs" -mindepth 1 -maxdepth 1 -type d | while read -r venv; do
  printf '%s\n' "$keep_venvs" | grep -qxF "$venv" || { log "pruning venv $venv"; rm -rf "$venv"; }
done

printf 'Quill release active: %s (%s)\n' "$release" "$(tr '\n' ' ' < "$release/RELEASE" 2>/dev/null || true)"
