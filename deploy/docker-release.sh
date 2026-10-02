#!/usr/bin/env bash
# Run as root INSIDE the dedicated Quill container (deploy-docker.sh streams the
# source in and runs this via `pct exec`). Nothing here touches the Proxmox host.
#
#   docker-release.sh /tmp/quill-src.tar.gz
#
# 1. unpack the source to /opt/quill/src/<ts>
# 2. build quill:<tag> (api/worker/diarizer) and quill-web:<tag> while the running
#    release keeps serving
# 3. import preflight in the new image; /etc/quill/quill.env and
#    /etc/quill/nginx-real-ip.conf on first install
# 4. stop worker (stages resume on restart) and api, pre-deploy SQLite snapshot
# 5. switch /opt/quill/.env to the new images, `python -m quill.db migrate`, start
#    api + web, health through nginx, start the worker and check it stays up
# 6. on any failure after the switch: restore the database snapshot if a migration
#    ran and restart the previous release (previous images, or the native systemd
#    units when this is the first Docker deploy)
# 7. keep the newest 3 image tags and source trees
set -euo pipefail
export LC_ALL=C

archive="${1:?Usage: docker-release.sh ARCHIVE.tar.gz}"
test "$(id -u)" = 0 || { echo 'docker-release.sh must run as root inside the CT' >&2; exit 1; }
test -f "$archive" || { echo "No archive at $archive" >&2; exit 1; }
test "$(hostname)" = quill || { echo "Refusing: this host is '$(hostname)', not quill" >&2; exit 1; }
command -v docker >/dev/null && docker compose version >/dev/null \
  || { echo 'Docker Engine with the compose plugin is required (deploy/install-docker.sh)' >&2; exit 1; }

exec 9>/run/lock/quill-deploy.lock
flock -n 9 || { echo 'Another Quill deployment is running' >&2; exit 1; }

ROOT=/opt/quill
DATA=/var/lib/quill
DB="$DATA/db/quill.sqlite3"
ENV_FILE=/etc/quill/quill.env
COMPOSE="$ROOT/compose.yaml"
KEEP=3
log() { printf '[quill-docker] %s\n' "$*" >&2; }
dc() { docker compose -f "$COMPOSE" --env-file "$ROOT/.env" "$@"; }

health() {  # health URL [attempts]
  local url="$1" attempts="${2:-45}"
  for _ in $(seq 1 "$attempts"); do
    if curl -fsS --max-time 3 -o /dev/null "$url"; then return 0; fi
    sleep 1
  done
  return 1
}

# ---------------------------------------------------------------------------
# 1. unpack
id quill >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin quill
uid="$(id -u quill)"; gid="$(id -g quill)"
install -d -m 0755 "$ROOT" "$ROOT/src"
install -d -m 0750 -g quill /etc/quill
install -d -o quill -g quill -m 0750 "$DATA"
for d in db uploads media backups cache cache/tmp models; do install -d -o quill -g quill -m 0700 "$DATA/$d"; done

ts="$(date -u +%Y%m%dT%H%M%SZ)"
src="$ROOT/src/$ts"
install -d -m 0755 "$src"
tar --no-same-owner -xzf "$archive" -C "$src"
rm -f "$archive"
for required in Dockerfile compose.yaml backend/pyproject.toml diarizer/requirements-lock-linux-x86_64-cpu.txt \
                frontend/package-lock.json deploy/nginx-docker.conf deploy/web-entrypoint.sh deploy/backup.py \
                deploy/quill.env.example deploy/quill-backup-docker.service deploy/quill-backup.timer; do
  test -f "$src/$required" || { echo "Source is missing $required" >&2; exit 1; }
done
sha="$(sed -n 's/^commit=\([^ ]*\).*/\1/p' "$src/RELEASE" 2>/dev/null)"; sha="${sha:-unknown}"
tag="$sha-$ts"
log "unpacked $(tr '\n' ' ' < "$src/RELEASE" 2>/dev/null) into $src"

# 2. build (the running release is untouched)
build() {  # build TARGET IMAGE
  docker build --pull --target "$1" -t "$2" \
    --build-arg QUILL_UID="$uid" --build-arg QUILL_GID="$gid" --build-arg QUILL_RELEASE="$sha" \
    "$src" > "$src/build-$1.log" 2>&1 \
    || { tail -30 "$src/build-$1.log" >&2; echo "docker build --target $1 failed (log: $src/build-$1.log)" >&2; exit 1; }
}
log "building quill:$tag"
build app "quill:$tag"
log "building quill-web:$tag"
build web "quill-web:$tag"

# 3. preflight + first-install configuration
docker run --rm --network none -e QUILL_DATA_DIR=/tmp/quill-preflight -e QUILL_SECRET_KEY=preflight-only \
  "quill:$tag" python -c 'import quill.app, quill.worker, quill.db, sys; print("import ok", sys.version.split()[0])' >&2
docker run --rm --network none "quill:$tag" test -x /opt/quill/diarizer-venv/bin/quill-diarize \
  || { echo 'image has no quill-diarize entry point' >&2; exit 1; }

if [ ! -f "$ENV_FILE" ]; then
  log "creating $ENV_FILE with a fresh QUILL_SECRET_KEY"
  secret="$(docker run --rm --network none "quill:$tag" python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  umask 077
  sed "s|^QUILL_SECRET_KEY=.*|QUILL_SECRET_KEY=$secret|" "$src/deploy/quill.env.example" > "$ENV_FILE.new"
  umask 022
  chown root:quill "$ENV_FILE.new"; chmod 0640 "$ENV_FILE.new"
  mv -f "$ENV_FILE.new" "$ENV_FILE"
fi
grep -Eq '^QUILL_SECRET_KEY=.{32,}' "$ENV_FILE" || { echo "$ENV_FILE has no QUILL_SECRET_KEY" >&2; exit 1; }
if [ ! -f /etc/quill/nginx-real-ip.conf ]; then
  log 'creating /etc/quill/nginx-real-ip.conf (commented; set your reverse proxy address)'
  cat > /etc/quill/nginx-real-ip.conf <<'NGINX'
# Trust X-Forwarded-For only from your TLS reverse proxy, so Quill sees real client IPs.
# Uncomment and set its address, then `docker compose -f /opt/quill/compose.yaml restart web`.
# set_real_ip_from 192.168.1.2;
# real_ip_header X-Forwarded-For;
# real_ip_recursive on;
NGINX
  chmod 0644 /etc/quill/nginx-real-ip.conf
fi
missing="$(grep -Eo '^QUILL_[A-Z0-9_]+' "$src/deploy/quill.env.example" | while read -r key; do
  grep -q "^$key=" "$ENV_FILE" || printf '%s ' "$key"; done)"
[ -z "$missing" ] || log "note: $ENV_FILE lacks $missing(defaults apply; compare with deploy/quill.env.example)"

# What is running now: a previous Docker release, the native systemd install, or nothing.
previous=none
if [ -f "$ROOT/.env" ] && [ -f "$COMPOSE" ]; then
  previous=docker; cp -p "$ROOT/.env" "$ROOT/.env.previous"; cp -p "$COMPOSE" "$ROOT/compose.yaml.previous"
elif systemctl is-enabled --quiet quill-api.service 2>/dev/null; then
  previous=native
fi
log "previous release: $previous"

# 4. stop, snapshot
log 'stopping worker (in-flight stage resumes on restart) and api'
case "$previous" in
  (docker) dc stop worker api || true ;;
  (native) systemctl stop quill-worker.service quill-api.service || true ;;
esac

snapshot=
if [ -f "$DB" ]; then
  if ! snapshot="$(docker run --rm --network none --user "$uid:$gid" -v "$DATA:$DATA" \
        -e QUILL_DATA_DIR="$DATA" "quill:$tag" python /app/deploy/backup.py --label pre-deploy --keep 5)"; then
    log 'pre-deploy backup failed; restarting the previous release unchanged'
    case "$previous" in
      (docker) dc start api worker ;;
      (native) systemctl start quill-api.service quill-worker.service ;;
    esac
    exit 1
  fi
  log "pre-deploy snapshot $snapshot"
fi

rollback() {
  trap - ERR
  set +e
  log "ROLLBACK: $1"
  dc logs --no-color --tail 40 api worker web 2>/dev/null
  dc down --remove-orphans 2>/dev/null
  if [ -n "$snapshot" ] && [ "${migrated:-0}" = 1 ]; then
    log "restoring database from $snapshot"
    rm -f "$DB-wal" "$DB-shm"
    runuser -u quill -- sh -c 'gzip -dc "$1" > "$2.restore" && mv -f "$2.restore" "$2"' _ "$snapshot" "$DB"
  fi
  case "$previous" in
    (docker)
      cp -p "$ROOT/.env.previous" "$ROOT/.env"; cp -p "$ROOT/compose.yaml.previous" "$COMPOSE"
      dc up -d --wait api web && dc up -d worker ;;
    (native)
      rm -f "$ROOT/.env"
      systemctl enable --now nginx.service quill-api.service quill-worker.service quill-backup.timer ;;
    (none)
      log "first install failed; nothing to roll back to. Source kept at $src" ;;
  esac
  if [ "$previous" != none ]; then
    if health http://127.0.0.1/api/auth/state 45; then log "rolled back to the $previous release (healthy)"
    else log "rolled back to the $previous release but it is NOT healthy either - investigate now"; fi
  fi
  exit 1
}

# 5. switch + migrate + start
migrated=0
trap 'rollback "unexpected error at line $LINENO"' ERR
install -m 0644 "$src/compose.yaml" "$COMPOSE"
umask 022
printf 'QUILL_IMAGE=quill:%s\nQUILL_WEB_IMAGE=quill-web:%s\nQUILL_UID=%s\nQUILL_GID=%s\n' "$tag" "$tag" "$uid" "$gid" > "$ROOT/.env"
dc config --quiet || rollback 'compose.yaml does not validate'

log 'running database migrations (python -m quill.db migrate)'
migrated=1
dc run --rm --no-deps -T api python -m quill.db migrate </dev/null || rollback 'migration failed'

if [ "$previous" = native ]; then
  log 'retiring the native install (nginx, quill-api, quill-worker, quill-backup.timer); files stay for rollback'
  systemctl disable --now quill-worker.service quill-api.service quill-backup.timer 2>/dev/null || true
  systemctl disable --now nginx.service || rollback 'could not stop native nginx'
fi
dc up -d --wait --wait-timeout 120 api web || rollback 'api/web did not become healthy'

# 6. health
health http://127.0.0.1/api/auth/state 30 || rollback 'nginx does not proxy /api/auth/state'
curl -fsS --max-time 5 -o /dev/null http://127.0.0.1/ || rollback 'nginx does not serve the frontend'
dc up -d worker || rollback 'worker failed to start'
sleep 10
[ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$(dc ps -q worker)")" = 'true 0' ] \
  || rollback 'worker exited or is restarting'
trap - ERR

# backup timer + account CLI run through the image now
install -m 0644 "$src/deploy/quill-backup-docker.service" /etc/systemd/system/quill-backup.service
install -m 0644 "$src/deploy/quill-backup.timer" /etc/systemd/system/quill-backup.timer
systemctl daemon-reload
systemctl enable --quiet --now quill-backup.timer
[ ! -f "$src/deploy/quill-users" ] || install -m 0755 "$src/deploy/quill-users" /usr/local/bin/quill-users

# 7. retention: newest tags plus whatever .env / .env.previous reference
keep="$(cat "$ROOT/.env" "$ROOT/.env.previous" 2>/dev/null | sed -n 's/^QUILL_\(WEB_\)\{0,1\}IMAGE=//p' | sort -u || true)"   # no .env.previous after a native -> docker switch
for repo in quill quill-web; do
  docker image ls "$repo" --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' | sort -k2 -r | awk '{print $1}' |
    tail -n +$((KEEP + 1)) | while read -r image; do
      printf '%s\n' "$keep" | grep -qxF "$image" || { log "removing old image $image"; docker image rm "$image" >/dev/null || true; }
    done
done
find "$ROOT/src" -mindepth 1 -maxdepth 1 -type d | sort -r | tail -n +$((KEEP + 1)) | xargs -r rm -rf
docker image prune -f >/dev/null

printf 'Quill release active: quill:%s (%s)\n' "$tag" "$(tr '\n' ' ' < "$src/RELEASE" 2>/dev/null || true)"
