#!/usr/bin/env bash
# shellcheck disable=SC2029  # values are validated locally and meant to expand client-side
# Create a FRESH, dedicated Quill container. Never run this against an existing CT;
# releases are installed with deploy-lxc.sh.
#
# Usage: deploy/provision-lxc.sh root@PROXMOX_HOST UNUSED_CTID [template] [storage] [ip]
#   template  default local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst
#             (must already be listed by `pveam list <template-storage>`; this script
#             never downloads a template, because that would write onto the host)
#   storage   default local-lvm (rootfs AND the /var/lib/quill mount point)
#   ip        empty/"dhcp" (default; add a DHCP reservation for the printed MAC)
#             or a static address in CIDR form, e.g. 192.168.1.50/24 (needs QUILL_NET_GW)
# Sizing and network are overridable through the environment:
#   QUILL_CORES=6 QUILL_MEMORY_MB=12288 QUILL_SWAP_MB=2048 QUILL_ROOTFS_GB=32
#   QUILL_DATA_GB=200 QUILL_DATA_BACKUP=0 QUILL_BRIDGE=vmbr0
#   QUILL_VLAN=        VLAN tag for net0 (empty: untagged)
#   QUILL_NET_GW=      gateway for a static ip
#   QUILL_NAMESERVER=  DNS server for the CT (empty: inherit the host's)
#   QUILL_SEARCHDOMAIN= DNS search domain (empty: inherit the host's)
#
# Everything this script installs lands INSIDE the container (apt, users, dirs).
# On the Proxmox host it only runs read-only checks plus `pct create/start/exec`.
set -euo pipefail
HOST="${1:?Usage: provision-lxc.sh root@PROXMOX_HOST UNUSED_CTID [template] [storage] [ip]}"
CTID="${2:?A verified unused cluster-wide CTID is required}"
TEMPLATE="${3:-local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
STORAGE="${4:-local-lvm}"
IP="${5:-dhcp}"
CORES="${QUILL_CORES:-6}"
MEMORY="${QUILL_MEMORY_MB:-12288}"
SWAP="${QUILL_SWAP_MB:-2048}"
ROOTFS_GB="${QUILL_ROOTFS_GB:-32}"
DATA_GB="${QUILL_DATA_GB:-200}"
DATA_BACKUP="${QUILL_DATA_BACKUP:-0}"
BRIDGE="${QUILL_BRIDGE:-vmbr0}"
VLAN="${QUILL_VLAN:-}"
GW="${QUILL_NET_GW:-}"
NAMESERVER="${QUILL_NAMESERVER:-}"
SEARCHDOMAIN="${QUILL_SEARCHDOMAIN:-}"

numeric() { case "$1" in (*[!0-9]*|'') echo "$2 must be numeric: '$1'" >&2; exit 2;; esac; }
numeric "$CTID" CTID; numeric "$CORES" QUILL_CORES; numeric "$MEMORY" QUILL_MEMORY_MB
numeric "$SWAP" QUILL_SWAP_MB; numeric "$ROOTFS_GB" QUILL_ROOTFS_GB; numeric "$DATA_GB" QUILL_DATA_GB
[ -z "$VLAN" ] || numeric "$VLAN" QUILL_VLAN
ipv4='([0-9]{1,3}\.){3}[0-9]{1,3}'
[ -z "$NAMESERVER" ] || printf '%s' "$NAMESERVER" | grep -Eq "^$ipv4\$" || { echo "QUILL_NAMESERVER must be an IPv4 address" >&2; exit 2; }
case "$SEARCHDOMAIN" in (*[!a-zA-Z0-9.-]*) echo 'Invalid QUILL_SEARCHDOMAIN' >&2; exit 2;; esac
case "$DATA_BACKUP" in (0|1) ;; (*) echo 'QUILL_DATA_BACKUP must be 0 or 1' >&2; exit 2;; esac
case "$TEMPLATE$STORAGE$BRIDGE" in (*[!a-zA-Z0-9_:./-]*) echo 'Invalid template/storage/bridge name' >&2; exit 2;; esac
case "$TEMPLATE" in (*:vztmpl/*) ;; (*) echo "Template must look like STORAGE:vztmpl/NAME, got '$TEMPLATE'" >&2; exit 2;; esac

case "$IP" in
  (''|dhcp) NET_IP='ip=dhcp' ;;
  (*)
    if ! printf '%s' "$IP" | grep -Eq "^$ipv4/([0-9]|[12][0-9]|3[0-2])\$"; then
      echo "ip must be 'dhcp' or an address in CIDR form like 192.168.1.50/24, got '$IP'" >&2; exit 2
    fi
    printf '%s' "$GW" | grep -Eq "^$ipv4\$" || { echo 'A static ip needs QUILL_NET_GW (the gateway address)' >&2; exit 2; }
    NET_IP="ip=$IP,gw=$GW" ;;
esac

ssh "$HOST" "bash -s -- '$CTID' '$TEMPLATE' '$STORAGE' '$NET_IP' '$CORES' '$MEMORY' '$SWAP' '$ROOTFS_GB' '$DATA_GB' '$DATA_BACKUP' '$BRIDGE' '$VLAN' '$NAMESERVER' '$SEARCHDOMAIN'" <<'REMOTE'
set -euo pipefail
export LC_ALL=C
ctid="$1"; template="$2"; storage="$3"; net_ip="$4"; cores="$5"; memory="$6"; swap="$7"
rootfs_gb="$8"; data_gb="$9"; data_backup="${10}"; bridge="${11}"; vlan="${12}"
nameserver="${13}"; searchdomain="${14}"
dns=()
[ -z "$nameserver" ] || dns+=(--nameserver "$nameserver")
[ -z "$searchdomain" ] || dns+=(--searchdomain "$searchdomain")
tag="${vlan:+,tag=$vlan}"

# --- read-only preflight on the host -------------------------------------
# Proxmox's cluster-wide check rejects IDs that exist on any node.
if [ "$(pvesh get /cluster/nextid --vmid "$ctid" 2>/dev/null)" != "$ctid" ]; then
  echo "CTID $ctid is already used somewhere in the cluster" >&2; exit 1
fi
template_storage="${template%%:*}"
if ! pveam list "$template_storage" | awk 'NR > 1 {print $1}' | grep -qxF "$template"; then
  echo "Template $template is not present on storage $template_storage." >&2
  echo "Available locally:" >&2
  pveam list "$template_storage" >&2 || true
  echo "Candidates to download (ask the owner first - this writes onto the host):" >&2
  pveam available --section system 2>/dev/null | grep -E 'ubuntu-24\.04' >&2 || true
  exit 1
fi
if ! pvesm status --storage "$storage" --enabled 1 >/dev/null 2>&1; then
  echo "Storage $storage is not an enabled storage on this node (see pvesm status)" >&2; exit 1
fi

# --- create ---------------------------------------------------------------
pct create "$ctid" "$template" --hostname quill --unprivileged 1 \
  --ostype ubuntu \
  --cores "$cores" --memory "$memory" --swap "$swap" \
  --rootfs "$storage:$rootfs_gb" \
  --mp0 "$storage:$data_gb,mp=/var/lib/quill,backup=$data_backup" \
  --features nesting=1 --onboot 1 "${dns[@]}" \
  --net0 "name=eth0,bridge=$bridge,firewall=1,$net_ip$tag,type=veth" \
  --description 'Quill meeting transcription - dedicated application container'
pct start "$ctid"

# --- inside the container only ---------------------------------------------
pct exec "$ctid" -- bash -s <<'IN_CT'
set -euo pipefail
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
for attempt in $(seq 1 60); do
  if getent hosts archive.ubuntu.com >/dev/null 2>&1; then break; fi
  if [ "$attempt" = 60 ]; then echo 'Container has no working DNS/network after 60 s' >&2; exit 1; fi
  sleep 1
done
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3.12-venv python3-dev ffmpeg sqlite3 curl ca-certificates \
  build-essential libsndfile1 nginx
id quill >/dev/null 2>&1 || useradd --system --home-dir /var/lib/quill --shell /usr/sbin/nologin quill
install -d -m 0755 /opt/quill /opt/quill/releases /opt/quill/venvs
install -d -m 0750 -g quill /etc/quill
install -d -o quill -g quill -m 0750 /var/lib/quill
install -d -o quill -g quill -m 0700 \
  /var/lib/quill/db /var/lib/quill/uploads /var/lib/quill/media \
  /var/lib/quill/backups /var/lib/quill/cache
install -d -m 0700 /var/cache/quill-pip
# The Quill site is installed by install-release.sh; drop the distro default page.
rm -f /etc/nginx/sites-enabled/default
systemctl enable nginx
systemctl restart nginx
IN_CT

echo "--- CT $ctid config"
pct config "$ctid"
echo "--- MAC for a DHCP reservation (quill):"
pct config "$ctid" | sed -n 's/^net0: .*hwaddr=\([0-9A-Fa-f:]*\).*/\1/p'
pct exec "$ctid" -- sh -c 'ip -4 -o addr show dev eth0'
REMOTE
echo "Provisioned CT $CTID on $HOST. Next: a DHCP reservation + DNS name, then deploy/deploy-lxc.sh $HOST $CTID"
