# Deploying Quill on Proxmox

Quill runs in one dedicated, unprivileged LXC on a Proxmox host, hostname
`quill`, CPU only. Everything is installed **inside** the container. Nothing is
written onto the Proxmox host: files are streamed with
`ssh root@PROXMOX_HOST "pct exec CTID -- sh -c 'cat > path'" < file`, never
`scp`'d to the host and never `pct push`'d from a host path.

In the examples below, replace `root@pve` with your Proxmox host and `<CTID>` with
the container id.

| File | Where it ends up (inside the CT) |
|---|---|
| `provision-lxc.sh` | run once from your workstation: creates the CT, apt packages, user, dirs |
| `deploy-lxc.sh` | run from your workstation for every release |
| `install-release.sh` | runs inside the CT (shipped in the release tarball) |
| `quill-api.service` | `/etc/systemd/system/` - uvicorn `quill.app:app` on 127.0.0.1:8020 |
| `quill-worker.service` | `/etc/systemd/system/` - `python -m quill.worker`, Nice=5, MemoryMax=9G (diarizer included) |
| `quill-backup.service` / `.timer` | nightly SQLite backup at 03:40 (+-20 min) |
| `backup.py` | SQLite backup API -> `/var/lib/quill/backups/quill-<label>-<ts>.sqlite3.gz` |
| `nginx-quill.conf` | `/etc/nginx/sites-available/quill` - :80, static dist + `/api` proxy |
| `quill.env.example` | template for `/etc/quill/quill.env` (0640 root:quill) |
| `quill-users` | `/usr/local/bin/` - account recovery from a shell (`list`, `reset-link`, `set-role`, `enable`); see `docs/ACCOUNTS.md` |

## Layout inside the CT

```
/opt/quill/releases/<UTC ts>/   backend/ diarizer/ frontend/dist/ deploy/ RELEASE
/opt/quill/current  -> releases/<ts>        /opt/quill/previous -> the one before
/opt/quill/venvs/backend-<hash>             hash of backend/pyproject deps + python version
/opt/quill/venvs/diarizer-<hash>            hash of diarizer/requirements.lock (torch CPU index)
/opt/quill/venv -> venvs/backend-<hash>     /opt/quill/diarizer-venv -> venvs/diarizer-<hash>
/etc/quill/quill.env                        settings; QUILL_SECRET_KEY generated on first install only
/etc/quill/nginx-real-ip.conf               which reverse proxy may set X-Forwarded-For (per install)
/var/lib/quill (mp0, 200 GB)                db/ uploads/ media/ backups/ cache/ models/
```

## 1. Before provisioning (read-only checks on the host)

```sh
ssh root@pve 'pvesm status'                    # pick the storage for rootfs + mp0 (default local-lvm)
ssh root@pve 'pveam list local'                # the Ubuntu 24.04 template must be listed
ssh root@pve 'pvesh get /cluster/nextid'       # candidate CTID
ssh root@pve 'ip -br link'                     # the bridge to attach (default vmbr0)
```

The script defaults to `local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst`
on `local-lvm`. If the template is missing it stops and lists candidates. It never
downloads one (that writes onto the host); run `pveam download` yourself if you
want it.

## 2. Provision

```sh
deploy/provision-lxc.sh root@pve <CTID> [template] [storage] [ip]
# DHCP (then add a reservation for the printed MAC):
deploy/provision-lxc.sh root@pve 170
# static, on VLAN 20, with explicit DNS:
QUILL_NET_GW=192.168.20.1 QUILL_VLAN=20 QUILL_NAMESERVER=192.168.20.1 QUILL_SEARCHDOMAIN=lan \
  deploy/provision-lxc.sh root@pve 170 local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst local-lvm 192.168.20.50/24
```

Defaults: 6 cores, 12288 MB RAM, 2048 MB swap, 32 GB rootfs, 200 GB `mp0` at
`/var/lib/quill`, `nesting=1`, `onboot=1`, `net0` on `vmbr0` with the firewall
flag, DNS inherited from the host. Override with `QUILL_CORES`,
`QUILL_MEMORY_MB`, `QUILL_SWAP_MB`, `QUILL_ROOTFS_GB`, `QUILL_DATA_GB`,
`QUILL_BRIDGE`, `QUILL_VLAN`, `QUILL_NET_GW`, `QUILL_NAMESERVER` and
`QUILL_SEARCHDOMAIN`.

`mp0` is created with `backup=0` (`QUILL_DATA_BACKUP=1` to include it), so vzdump
does not copy hundreds of GB of media. The SQLite backups also live on `mp0`, so
copy `/var/lib/quill/backups` somewhere else if you need off-box copies.

The script prints the CT's MAC address at the end. Give the container a stable
address (a DHCP reservation, or the static IP above) and, ideally, a LAN DNS name.

## 3. Configure

The first deploy creates `/etc/quill/quill.env` from `quill.env.example` with a
fresh `QUILL_SECRET_KEY`. Then set at least:

- `QUILL_GATEWAY_URL`: your OpenAI-compatible model gateway (see the main README
  for what it must provide).
- `QUILL_PUBLIC_URL`: the https address people use, e.g. `https://quill.example.com`.
- `/etc/quill/nginx-real-ip.conf`: uncomment it and set your reverse proxy's
  address, so rate limits and the LAN-only first-run setup see real client IPs.

Restart after editing: `systemctl restart quill-api quill-worker` (and
`nginx -t && systemctl reload nginx` for the real-IP file).

## 4. Deploy a release

```sh
deploy/deploy-lxc.sh root@pve <CTID>                   # tests + build + install
deploy/deploy-lxc.sh --frontend-only root@pve <CTID>   # publish frontend/dist only, no restarts
APP_URL=https://quill.example.com deploy/deploy-lxc.sh root@pve <CTID>   # plus public checks
```

Options: `--allow-dirty` ships tracked but uncommitted changes (via
`git stash create`, never untracked files), and `--skip-tests` skips the tests.

What `install-release.sh` does inside the CT:

1. Unpacks the release to `/opt/quill/releases/<ts>` and verifies the layout.
2. Builds a venv only when its hash is new. The old release keeps running during
   this step. The diarizer venv is installed from the lock with
   `--extra-index-url https://download.pytorch.org/whl/cpu`, and the `diarizer/`
   package itself is reinstalled with `--no-deps` on each release.
3. Runs an import preflight with the new venv, creates `/etc/quill/quill.env` and
   `/etc/quill/nginx-real-ip.conf` on first install, installs the units and the
   nginx site, and runs `nginx -t`.
4. Stops the worker, which resumes its checkpointed stage on restart, then stops
   the API and takes a `pre-deploy` SQLite snapshot (the newest 5 are kept).
5. Switches `current`, `venv` and `diarizer-venv`, then runs
   `python -m quill.db migrate` as `quill`.
6. Starts the API and checks `GET /api/auth/state` on :8020 and through nginx,
   then starts the worker and checks it stays up.
7. On any failure it restores the previous symlinks, units and nginx site, puts
   back the pre-deploy database snapshot if a migration had started, and
   restarts the old release.
8. Keeps the newest 3 releases (plus `current` and `previous`) and the venvs
   they reference.

Frontend-only publishes assets first and `index.html` last into the current
release. Older hashed assets stay in place for tabs that are already open.

## 5. Public access through a reverse proxy

Any TLS reverse proxy works. With Nginx Proxy Manager (or NPMplus), add a proxy host:

- Domain name: `quill.example.com`
- Scheme `http`, forward to the container's address, port `80`
- Block common exploits: on. **Websockets support: on**
- SSL: a Let's Encrypt certificate, **Force SSL** on, HTTP/2 on, HSTS on
- Advanced -> Custom Nginx configuration:

```nginx
client_max_body_size 128m;
proxy_request_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
proxy_buffering off;
```

tus sends 64 MB chunks, so no single request is near 10 GB. `proxy_buffering off`
keeps SSE (`/api/events`) and large media range responses streaming. Avoid CDNs
that cap request bodies below 128 MB. Then put the proxy's address in
`/etc/quill/nginx-real-ip.conf` (step 3).

Check: `curl -sS https://quill.example.com/api/auth/state` returns `{"needs_setup": true}`
on a fresh install. Then open the site **from your LAN** (first-run setup is
refused from public addresses) and create the admin account.

## Operations

```sh
ssh root@pve "pct exec <CTID> -- journalctl -u quill-api -u quill-worker -f"
ssh root@pve "pct exec <CTID> -- systemctl start quill-backup.service"   # backup now
ssh root@pve "pct exec <CTID> -- ls -l /var/lib/quill/backups"
ssh root@pve "pct exec <CTID> -- /usr/local/bin/quill-users reset-link you@example.com"  # locked-out admin
```

**Manual rollback**:
1. Stop both services: `systemctl stop quill-worker quill-api`.
2. Point `/opt/quill/current` at `/opt/quill/previous`, and repoint `venv` and
   `diarizer-venv` at the venvs listed in that release's `.venv-backend` and
   `.venv-diarizer` files.
3. If the schema changed, restore the matching `quill-pre-deploy-*.sqlite3.gz`:
   `gunzip -c ... > /var/lib/quill/db/quill.sqlite3` as `quill`, after removing
   the `-wal` and `-shm` files.
4. Start both services again.
