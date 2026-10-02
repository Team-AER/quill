#!/usr/bin/env bash
# Run as root INSIDE the Quill container: install Docker Engine + the compose
# plugin from Docker's apt repository for this Ubuntu release. Idempotent.
# Works in an unprivileged LXC with `nesting=1` (overlayfs storage driver).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
test "$ID" = ubuntu || { echo "install-docker.sh supports Ubuntu (found $ID)" >&2; exit 1; }
apt-get update -q >/dev/null
apt-get install -y -q ca-certificates curl >/dev/null
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<SOURCES
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $VERSION_CODENAME
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
SOURCES
apt-get update -q >/dev/null
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
systemctl enable --now docker.service >/dev/null
docker run --rm hello-world >/dev/null
echo "Docker $(docker version --format '{{.Server.Version}}') ready ($(docker info --format '{{.Driver}}'))"
