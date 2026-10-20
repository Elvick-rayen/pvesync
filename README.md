# netbox-pve-sync

One-way, event-driven synchronization of **Proxmox VE virtual machines into NetBox**.

Proxmox is the source of truth. NetBox is the inventory. Changes made in NetBox never
touch Proxmox. The agent only *reads* from Proxmox (its Proxmox token is read-only).

```
NetBox 192.168.1.218:8000
 ├── cluster PVE-Cyprus      <- agent on the PVE-Cyprus node(s)
 │    ├── web01  (eth0 192.168.1.50/24, Management IPv4)
 │    └── db01   (eth0 192.168.1.51/24, Management IPv4)
 ├── cluster PVE-Production  <- agent on the PVE-Production node(s)
 └── cluster PVE-Lab         <- agent on the PVE-Lab node(s)
```

Supported: **Proxmox VE 8 and 9**, **NetBox 3.3 – 4.x** (tested against 3.3.10 and 4.7.2),
Python 3.11+ (PVE 8 ships 3.11, PVE 9 ships 3.13).

---

## Contents

1. [What is synchronized](#what-is-synchronized)
2. [Architecture](#architecture)
3. [Requirements](#requirements)
4. [Step 1 - Prepare NetBox](#step-1---prepare-netbox)
5. [Step 2 - Install on a Proxmox node](#step-2---install-on-a-proxmox-node)
6. [Step 3 - First dry run](#step-3---first-dry-run)
7. [Step 4 - Initial synchronization and service start](#step-4---initial-synchronization-and-service-start)
8. [QEMU Guest Agent](#qemu-guest-agent)
9. [How the management IP is chosen](#how-the-management-ip-is-chosen)
10. [Configuration reference](#configuration-reference)
11. [CLI](#cli)
12. [Event sync, retry queue and reconciliation](#event-sync-retry-queue-and-reconciliation)
13. [Ownership and safety](#ownership-and-safety)
14. [IP addresses](#ip-addresses)
15. [Disk, RAM and CPU calculation](#disk-ram-and-cpu-calculation)
16. [Logs and systemd](#logs-and-systemd)
17. [Troubleshooting](#troubleshooting)
18. [Adding another Proxmox environment](#adding-another-proxmox-environment)
19. [Upgrading](#upgrading)
20. [Uninstall](#uninstall)
21. [Security considerations](#security-considerations)
22. [Development and tests](#development-and-tests)
23. [Known limitations](#known-limitations)

More detail: [docs/architecture.md](docs/architecture.md) (design and research notes),
[docs/testing.md](docs/testing.md) (how everything was validated).

---

## What is synchronized

For every eligible Proxmox VM (QEMU VMs; templates are skipped by default):

| NetBox field            | Value                                                               |
|-------------------------|---------------------------------------------------------------------|
| Name                    | Proxmox VM name                                                     |
| Cluster                 | the NetBox cluster configured for this Proxmox environment          |
| Site                    | the configured `netbox.site` (e.g. `inmind.ai`); unset = left alone |
| Status                  | power state: running → **Active**, stopped → **Offline**            |
| vCPUs                   | configured vCPUs (`vcpus`, else `sockets × cores`)                  |
| Memory                  | configured memory in MB (not current usage)                         |
| Disk                    | sum of the configured sizes of all virtual disks                    |
| Interface               | always one interface named **`eth0`**                               |
| IP address              | the management IPv4 from the QEMU Guest Agent, assigned to `eth0`   |
| Primary IPv4            | the same IP (shown as "Primary IPv4" in NetBox)                     |
| Custom fields           | `proxmox_vmid`, `proxmox_source` (identity and ownership)           |
| Tag                     | `tag`                                                               |

The physical Proxmox **node** a VM runs on is **not** stored in NetBox (the VM's `device`
field stays empty). Storage (Ceph, NFS, local-lvm, ZFS) is not represented either.

## Architecture

```
            Proxmox VE (127.0.0.1:8006, read-only token)
                 │  /cluster/tasks      (task log: create, config, resize, destroy, start ...)
                 │  /cluster/resources  (VM list: appear / vanish / rename / RAM / CPU / disk)
                 ▼
   ┌───────────────────────── netbox-pve-sync (systemd) ─────────────────────────┐
   │  event detector  ──►  persistent queue (SQLite)  ──►  sync engine           │
   │      every 5 s            one row per VMID              read Proxmox VM     │
   │                           exponential backoff           compare with NetBox │
   │  local check (10 min) ──►  (catches guest IP changes)   write only the diff │
   │  reconciliation (6 h + at startup) ── full Proxmox ⇄ NetBox comparison      │
   └─────────────────────────────────────────────────────────────────────────────┘
                 │  REST API (token)
                 ▼
            NetBox
```

* **Event-based.** Proxmox VE 8 and 9 have *no* push API for VM lifecycle events. The agent
  watches the same two documented endpoints the Proxmox web UI watches: the cluster task
  log and the cluster resource list. Both are local, cheap, read-only calls. NetBox is only
  contacted when something changed. See [docs/architecture.md](docs/architecture.md).
* **Level-triggered.** An event only says *which* VM to look at. The sync always reads the
  VM's *current* state, so duplicate, late or out-of-order events cannot cause damage.
* **Idempotent.** Read → compare → write only what differs. Syncing VM 101 four times gives
  one VM, one `eth0`, one IP. The last three runs make zero NetBox writes.
* **Persistent retry queue.** If NetBox is down, the VMID stays in the SQLite queue and is
  retried with exponential backoff (5 s → 1 h) - across restarts and reboots.
* **Reconciliation** every 6 h and at every start repairs anything missed.
* **Clusters.** In a multi-node PVE cluster you may install the agent on every node; only
  one (the first online node) is active, the others stand by and take over automatically.

## Requirements

* Proxmox VE 8.x or 9.x node (Debian 12 / 13). Root shell for installation.
* Python 3.11+ (already on PVE), `python3-requests`, `python3-yaml` (installed by `install.sh`).
* Network access from the node to NetBox.
* NetBox 3.3 or newer, with an API token (see below).
* The QEMU Guest Agent in each VM for IP discovery (VMs without it are still synced, without IP).

## Step 1 - Prepare NetBox

Do this **once per NetBox**, not per Proxmox node.

### 1.1 Create the cluster(s)

The agent never creates clusters. In NetBox:

1. *Virtualization → Cluster Types → Add*: e.g. `Proxmox`.
2. *Virtualization → Clusters → Add*: name **exactly** as you will configure it, e.g. `PVE-Cyprus`.

If the configured cluster does not exist, the agent refuses to start with a clear error.

The site set on the VMs (`netbox.site`, e.g. `inmind.ai`) must exist as well (*Organization →
Sites*); the agent never creates sites. NetBox only accepts a VM on a site if its cluster is on
that site too or on no site at all, so if you assign the cluster to a site, use the same one.

### 1.2 Create a user for the agent with minimum permissions

In *Admin → Users* (NetBox 4.x) or *Admin → Users* in the Django admin (NetBox 3.x) create a
user `netbox-pve-sync` (not staff, not superuser). Then add these **object permissions**
(*Admin → Permissions*), all assigned to that user:

| Permission name                   | Object types                                             | Actions                     |
|-----------------------------------|----------------------------------------------------------|-----------------------------|
| netbox-pve-sync: VMs and IPs      | Virtualization › virtual machine, IPAM › IP address      | view, add, change, delete   |
| netbox-pve-sync: interfaces       | Virtualization › interface                               | view, add                   |
| netbox-pve-sync: read-only lookups| Virtualization › cluster, DCIM › site, Extras › tag, Extras › custom field, IPAM › VRF | view |

Why each one is needed:

* **VMs**: create/update/delete the VMs it owns, set Primary IPv4, site and status.
* **IP addresses**: create the management IP, assign/unassign it, delete IPs it created.
* **Interfaces**: create `eth0`. No change/delete needed: when a VM is deleted NetBox deletes
  its interfaces itself (cascade).
* **Lookups**: find the cluster, the site, the tag, the custom fields and (optional) VRF by name.

The agent does **not** need admin rights. (Optionally restrict the VM permission with a
constraint such as `{"cluster__name": "PVE-Cyprus"}` - one user per environment - for even
stronger isolation.)

### 1.3 Create the API token

Log in as an admin and create a token **for the `netbox-pve-sync` user**
(*Admin → API Tokens → Add*, choose the user, keep **Write enabled** ticked).

* NetBox < 4.5: a 40-character token; the agent sends `Authorization: Token <key>`.
* NetBox 4.5+: v2 tokens look like `nbt_<key>.<secret>`; the agent detects this and sends
  `Authorization: Bearer ...`. Copy the token right away - NetBox shows it only once.

### 1.4 Create the custom fields and tag

The agent needs two custom fields on *Virtual Machine* and one tag:

| Name             | Type    | Purpose                                         |
|------------------|---------|-------------------------------------------------|
| `proxmox_vmid`   | Integer | stable identity (the Proxmox VMID)              |
| `proxmox_source` | Text    | which Proxmox environment owns the VM           |
| tag `tag`             | -  | marks objects managed by the agent              |

Easiest: after installing on the first node (step 2), run **once** with an *admin* token:

```bash
NETBOX_API_TOKEN=<admin-token> netbox-pve-sync init-netbox --dry-run   # preview
NETBOX_API_TOKEN=<admin-token> netbox-pve-sync init-netbox
```

(The real environment variable wins over `secrets.env`, so the admin token is used only for
this one command and is never stored. To keep it out of your shell history, type
`read -rs NETBOX_API_TOKEN && export NETBOX_API_TOKEN` first, then run the commands without the
`NETBOX_API_TOKEN=...` prefix, and `unset NETBOX_API_TOKEN` afterwards.) Or create them by hand in *Customization → Custom
Fields* and *Customization → Tags*.

## Step 2 - Install on a Proxmox node

```bash
# on the Proxmox node, as root
apt-get install -y git
git clone https://github.com/Elvick-rayen/pvesync.git netbox-pve-sync
cd netbox-pve-sync
./install.sh --netbox-url http://192.168.1.218:8000 --cluster PVE-Cyprus --site inmind.ai
```

The installer asks for the NetBox API token (input hidden). It then:

1. checks the OS (Debian), Proxmox version (8 or 9), Python (3.11+) and systemd;
2. installs `python3-requests` / `python3-yaml` via apt only if missing;
3. creates the system user `netbox-pve-sync` (no login shell);
4. installs the code to `/opt/netbox-pve-sync` and the command `/usr/local/bin/netbox-pve-sync`;
5. creates `/etc/netbox-pve-sync/config.yaml` and `secrets.env` (mode 0640, root:netbox-pve-sync);
6. creates a **read-only** Proxmox role `NetBoxSync` (`VM.Audit`, `Sys.Audit` and
   `VM.GuestAgent.Audit` on PVE 9 / `VM.Monitor` on PVE 8), user `netbox-sync@pve` (no
   password) and one API token per node `netbox-sync@pve!agent-<node>`;
7. copies the cluster CA (`/etc/pve/pve-root-ca.pem`) so TLS to `127.0.0.1:8006` is verified;
8. installs and enables the systemd service, runs `test-connection`;
9. asks whether to start the service (default: no).

Nothing else on the Proxmox host is changed. Options: `./install.sh --help`
(`--start`, `--no-start`, `--skip-pve-token`, `--force`).

Expected `test-connection` output:

```
[ OK ] Proxmox VE 9.1.1 - cluster (standalone), node pve1, 12 QEMU VM(s) visible
[ OK ] Proxmox token privileges: Sys.Audit, VM.Audit, VM.GuestAgent.Audit
[ OK ] NetBox 3.3.10 reachable (VM disk unit: GB)
[ OK ] NetBox token accepted
[ OK ] NetBox cluster 'PVE-Cyprus' exists (id 1)
[ OK ] NetBox site 'inmind.ai' exists (id 1)
[ OK ] NetBox custom fields and tag present

All checks passed.
```

## Step 3 - First dry run

Before anything is written, preview:

```bash
netbox-pve-sync --dry-run          # = netbox-pve-sync reconcile --dry-run
```

It reads Proxmox and NetBox and prints every change it *would* make, e.g.:

```
Reconciliation plan (nothing was changed):
  VM 101: would be created - created VM; created interface eth0; created IP 192.168.1.50/24; primary_ip4 -> 192.168.1.50/24
  created: 3   updated: 0   unchanged: 0   deleted: 0
  skipped VM 9000: template
  no management IP for VM 103: agent_not_running (existing NetBox IP is kept)

DRY RUN: 11 NetBox API write(s) would be made (nothing was changed):
  CREATE  VM web01 (vmid 101)
  ...
```

Check name conflicts (`CONFLICT VM ...`) here: a NetBox VM with the same name that the agent
does not own is never overwritten (see `sync.adopt_existing`).

## Step 4 - Initial synchronization and service start

Either let the service do it (it reconciles at every start):

```bash
systemctl start netbox-pve-sync
journalctl -u netbox-pve-sync -f
```

or run it by hand first and watch the result:

```bash
netbox-pve-sync reconcile
systemctl start netbox-pve-sync
```

Existing VMs do not need to be recreated - reconciliation picks all of them up.

## QEMU Guest Agent

IP addresses come **only** from the QEMU Guest Agent (through the Proxmox API,
`agent/network-get-interfaces`). For each VM:

1. In the VM: install and start the agent
   (`apt install qemu-guest-agent && systemctl enable --now qemu-guest-agent`,
   on Windows the virtio-win guest tools).
2. In Proxmox: *VM → Options → QEMU Guest Agent → Enabled* (`qm set <vmid> --agent 1`),
   then fully stop/start the VM once (a reboot inside the guest is not enough).

If the agent is missing, disabled, not running, or reports no usable IPv4:

* the VM is still created/updated in NetBox (name, CPU, RAM, disk, `eth0`);
* an existing NetBox IP is **kept** - never deleted because the agent is temporarily down;
* a warning is logged and the VM is re-checked (30 s, backing off to 15 min, for 24 h; after
  that the 10-minute local check keeps looking).

Check a VM: `netbox-pve-sync explain-ip --vmid 101`.

## How the management IP is chosen

Fully configurable under `ip_selection`. `explain-ip` shows each step.

1. Collect every address the guest agent reports.
2. **Reject** an address if (first match is the reason):
   * it is not IPv4 (IPv6 is never the management address);
   * it is malformed;
   * its interface matches `exclude_interfaces` - defaults: `lo`, `docker*`, `br-*`, `veth*`,
     `virbr*`, `lxcbr*`, `podman*`, `cni*`, `flannel*`, `cali*`, `cilium*`, `kube-*`, `vxlan*`,
     `tun*`, `tap*`, `wg*`, `tailscale*`, `zt*`, ...;
   * it lies in `exclude_networks` - defaults: `127.0.0.0/8`, `169.254.0.0/16`,
     `172.17.0.0/16` (Docker default bridge), `0.0.0.0/8`, multicast, reserved;
   * it is the network/broadcast address of its subnet;
   * `require_pve_mac: true` and the interface's MAC is not one of the VM's Proxmox NICs.
3. **Rank** the rest: `prefer_networks` order → `prefer_interfaces` order → interfaces whose MAC
   belongs to a Proxmox NIC (`net0` first; guest-internal virtual interfaces have unknown MACs)
   → the order the guest agent reported.
4. The best one is the management IPv4, with its prefix length (e.g. `192.168.1.50/24`).

Tip: if your management network is known, set `prefer_networks: ["192.168.1.0/24"]`.

## Configuration reference

`/etc/netbox-pve-sync/config.yaml` - see [config.example.yaml](config.example.yaml) for all
options with comments. Secrets are referenced as `${NAME}` and read from the environment or
`/etc/netbox-pve-sync/secrets.env` ([.env.example](.env.example)). Unknown options are
rejected, so typos are caught. Validate with `netbox-pve-sync check-config`.

Usually only these differ between Proxmox environments:

```yaml
netbox:
  url: "http://192.168.1.218:8000"
  token: "${NETBOX_API_TOKEN}"
  cluster: "PVE-Cyprus"
proxmox:
  token_id: "netbox-sync@pve!agent-pve1"   # written by install.sh
  token_secret: "${PVE_TOKEN_SECRET}"
```

Main options:

| Option | Default | Meaning |
|---|---|---|
| `netbox.cluster` | - | NetBox cluster of this environment (must exist) |
| `netbox.site` | none | NetBox site (name or slug) set on every VM, e.g. `inmind.ai`; none = site left alone |
| `netbox.vrf` | none | VRF for created IPs (none = global table) |
| `netbox.tag` | `tag`             | tag that marks managed objects |
| `proxmox.verify_ssl` / `ca_bundle` | `true` / cluster CA | TLS verification of the Proxmox API |
| `proxmox.leader_election` | `true` | only one active agent per PVE cluster |
| `proxmox.agent_nodes` | all | set if the agent is not installed on every node |
| `sync.source_id` | = cluster | ownership marker stored in `proxmox_source` |
| `sync.event_poll_interval` | `5s` | how often the task log / VM list are checked (local only) |
| `sync.local_check_interval` | `10m` | Proxmox-only re-check of all VMs (guest IP changes) |
| `sync.reconciliation_interval` | `6h` | full Proxmox ⇄ NetBox comparison |
| `sync.include_templates` | `false` | also sync templates |
| `sync.exclude_vmids` / `exclude_pve_tags` | `[]` | never sync these VMs |
| `sync.adopt_existing` | `false` | take over an *unmanaged* NetBox VM with the same name |
| `sync.claim_unassigned_ips` | `true` | reuse an existing *unassigned* IP object |
| `sync.ip_cleanup_policy` | `delete_managed` | see [IP addresses](#ip-addresses) |
| `sync.sync_status` | `true` | NetBox status follows the power state (running → Active, stopped → Offline) |
| `sync.max_deletions_per_run` | `10` | reconciliation refuses to delete more VMs at once |
| `retry.initial_delay` / `max_delay` | `5s` / `1h` | exponential backoff of the retry queue |
| `guest_agent.ip_wait_*` | `30s`/`15m`/`24h` | re-check schedule for VMs without IP |
| `logging.level` / `format` | `INFO` / `text` | `DEBUG` shows every NetBox call; `json` for log shippers |

Durations: `500ms`, `30s`, `10m`, `6h`, `1d`.

## CLI

```
netbox-pve-sync version
netbox-pve-sync check-config               # validate the config file
netbox-pve-sync test-connection            # Proxmox + NetBox reachability, tokens, permissions, prerequisites
netbox-pve-sync init-netbox [--dry-run]    # create custom fields + tag (admin token)
netbox-pve-sync --dry-run                  # preview a full reconciliation
netbox-pve-sync reconcile [--dry-run] [--allow-mass-delete]
netbox-pve-sync sync --vmid 101 [--vmid 102] [--dry-run]
netbox-pve-sync explain-ip --vmid 101      # how the management IP is chosen
netbox-pve-sync status [-v]                # service state, last reconcile, retry queue, VMs waiting for IP
netbox-pve-sync queue [--retry-now | --clear]
netbox-pve-sync run                        # the daemon (used by systemd)
```

Run as root or as the service user; as root the wrapper switches to `netbox-pve-sync`
automatically so the state file keeps the right owner. Exit codes: `0` ok, `1` error /
conflict, `78` configuration error.

## Event sync, retry queue and reconciliation

| What happens in Proxmox | Detected by | Delay |
|---|---|---|
| create, clone, restore, destroy | task log + VM list | ~5 s |
| CPU / RAM / boot disk / name change (GUI, API, `qm set`) | task log and/or VM list | ~5 s |
| extra disk added/removed with `qm set` on the CLI (no task) | local check | ≤ 10 min |
| disk resize, move | task log | ~5 s |
| start, reboot, migrate | task log → status + IP re-check | ~5 s, IP when the guest agent reports it |
| stop, shutdown | task log + VM list → status Offline | ~5 s (the last known IP is kept) |
| IP changed inside the guest | local check | ≤ 10 min |
| anything missed while the agent was down | startup reconciliation | at start |
| anything else | reconciliation | ≤ 6 h |

**Retry queue** (`/var/lib/netbox-pve-sync/state.db`, SQLite): one row per VMID, so a burst
of events becomes one sync. NetBox unreachable → backoff 5 s, 10 s, 20 s, ... up to 1 h
(with jitter), logged as `NetBox unavailable; synchronization queued`. Conflicts that need a
human (name clash, IP owned by someone else) are retried slowly, starting at 1 minute.
Survives restarts and reboots. `netbox-pve-sync status` shows it.

**What SQLite stores**: the retry queue, VMs waiting for an IP, a fingerprint of the last
synced state per VM (so unchanged VMs cause zero NetBox calls), the last seen Proxmox VM list
and handled task IDs (to detect changes across restarts), and a few timestamps. NetBox stays
the inventory; the file can be deleted at any time and the next reconciliation rebuilds it.

**Reconciliation**: lists all Proxmox VMs and all NetBox VMs owned by this agent, creates
missing ones, updates stale ones, and deletes owned NetBox VMs whose VMID no longer exists in
Proxmox. If more than `max_deletions_per_run` deletions would happen (e.g. a wrong token
hides all VMs) it deletes nothing and logs an error; use `reconcile --allow-mass-delete`
after checking. An empty Proxmox VM list is never treated as "all VMs deleted".

## Ownership and safety

A NetBox VM is managed by an agent **only if all** of these are true:

1. it is in the agent's configured cluster;
2. it has the tag `tag`;
3. its `proxmox_source` equals the agent's `sync.source_id`;
4. its `proxmox_vmid` is set.

Everything else - VMs you created by hand, VMs of other Proxmox environments - is never
modified or deleted. The VM is identified by VMID (not name), so renames update the existing
NetBox VM. If a crash ever leaves two owned NetBox VMs for one VMID, the oldest is kept and
the duplicate removed.

Name clash: if a NetBox VM with the same name exists in the cluster but is not owned, the
agent logs an error and does not touch it. Set `sync.adopt_existing: true` to take over such
VMs (only ones without another `proxmox_vmid`/`proxmox_source`).

## IP addresses

Operation order (NetBox validation requires it): VM → `eth0` → IP (created already assigned
to `eth0`, or an existing IP assigned) → Primary IPv4 → only then the old IP is removed
(NetBox refuses to unassign an IP that is still a primary IP).

* **Existing IP:** if the address already exists in NetBox and is assigned to this VM it is
  reused; if it is *unassigned* it is claimed (`claim_unassigned_ips`); if it is assigned to
  **another object**, it is **not taken** - a warning names the holder and the VM is retried
  slowly until you fix it.
* **IP change:** the new IP becomes primary, then the old one is handled by the cleanup policy.
* **Deleting a VM:** NetBox deletes a VM's interfaces and the IPs on them (cascade). The agent
  therefore first detaches every IP it should keep.
* **`ip_cleanup_policy`**:
  * `delete_managed` (default): IPs that the agent created (tagged `tag`) are
    deleted when they are no longer used. IPs it did not create (e.g. claimed from IPAM, or
    added by hand) are only unassigned, never deleted.
  * `unassign`: no IP object is ever deleted; unused IPs are just unassigned.
* Extra IPs that someone added by hand to `eth0` are left alone.

## Disk, RAM and CPU calculation

* **Disk** = sum of `size=` of every attached virtual disk (`scsiN`, `virtioN`, `sataN`,
  `ideN`). Not counted: CD-ROMs, `unusedN` volumes, snapshots, and by default `efidisk0` /
  `tpmstate0` (`include_efi_tpm_disks`). This is the capacity the guest sees - not space used on
  the storage, not the storage pool size. Proxmox sizes are binary (`32G` = 32 GiB).
  NetBox ≤ 3.x stores VM disk in GB → `100G` becomes `100`; NetBox 4.x stores MB → `102400`
  (displayed as 100 GB). If someone added NetBox *virtual disk* objects to a VM (NetBox 4),
  NetBox computes the disk itself and the agent leaves it alone.
* **Memory** = the configured `memory` option in MiB (default 512). Balloon minimum and runtime
  usage are ignored. Example: configured 8192, using 5321 → NetBox 8192.
* **vCPUs** = `vcpus` if set, else `sockets × cores`. Host CPU usage is never synced.

## Logs and systemd

```bash
systemctl status netbox-pve-sync
systemctl restart netbox-pve-sync
journalctl -u netbox-pve-sync -f              # follow
journalctl -u netbox-pve-sync --since today
journalctl -u netbox-pve-sync -p warning      # only problems
netbox-pve-sync status -v
```

Example log lines:

```
INFO    Connected to Proxmox VE 9.1.1 pve_version=9.1.1 pve_major=9 cluster=(standalone) node=pve1 ...
INFO    Connected to NetBox 3.3.10 netbox_version=3.3.10 url=http://192.168.1.218:8000 cluster=PVE-Cyprus ... token="a36b…(40 chars)"
INFO    VM 101 synchronized successfully vmid=101 name=web01 outcome=created changes="created VM,created interface eth0,..."
WARNING QEMU Guest Agent unavailable for VM 103; keeping the last known NetBox IP vmid=103 status=agent_not_running
WARNING NetBox unavailable; synchronization queued queued=1 retry_in=10s attempt=2
INFO    VM 101 synchronized after retry vmid=101 attempts=3
```

Tokens are never logged (only the first 4 characters and the length); every log line also
passes through a filter that masks the configured secrets.

The unit ([systemd/netbox-pve-sync.service](systemd/netbox-pve-sync.service)): starts at boot
after the network and `pveproxy`, restarts after crashes (15 s, max 10 times in 10 min), but
not after a configuration error (exit 78 - fix the config, then restart). SIGTERM/SIGINT stop it
cleanly after the current API call. It runs as the unprivileged user `netbox-pve-sync` with no
capabilities, a read-only file system except its state directory, and a syscall filter
(`systemd-analyze security netbox-pve-sync` scores 1.2 "OK"). **Root is not needed**: the agent
only talks HTTPS to the local Proxmox API with its own token.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `NetBox cluster 'X' does not exist` | create it in NetBox or fix `netbox.cluster` (case-sensitive) |
| `NetBox site 'X' (netbox.site) does not exist` | create the site, or use its exact name or slug in `netbox.site` |
| `cluster ... is assigned to another site` | assign the cluster to the same site in NetBox (or to none), or fix `netbox.site` |
| `NetBox is not prepared: custom field ... does not exist` | `init-netbox` with an admin token (step 1.4) |
| `NetBox rejected the token` / HTTP 403 | token wrong, expired, not *write enabled*, or permissions missing (step 1.2) |
| `Proxmox token lacks VM.GuestAgent.Audit` (PVE 9) | upgraded from PVE 8? re-run `./install.sh` - it updates the role |
| VM has no IP in NetBox | `explain-ip --vmid N`; enable the guest agent option, install the agent in the guest, stop/start the VM |
| wrong IP chosen | set `ip_selection.prefer_networks` or `exclude_interfaces`, then `sync --vmid N` |
| `IP conflict: address belongs to another object` | the IP is assigned to another device/VM in NetBox; fix that object (the agent will not steal it) |
| `Cannot create VM in NetBox: name conflict` | a non-managed NetBox VM has that name: delete/rename it or set `adopt_existing: true` |
| `BLOCKED deletion of N VM(s)` | check why so many VMs are missing; then `reconcile --allow-mass-delete` |
| TLS error to Proxmox | custom certificate on 8006: set `proxmox.api_url` to its hostname or its CA in `ca_bundle` |
| Service does not start, exit 78 | `netbox-pve-sync check-config` and `test-connection` |
| Want more detail | `logging.level: DEBUG` (shows every NetBox call), restart |

## Adding another Proxmox environment

1. In NetBox create the cluster, e.g. `PVE-Production` (step 1.1). The user, token, custom
   fields and tag are shared - nothing else to do in NetBox.
2. On the new Proxmox node:
   ```bash
   git clone https://github.com/Elvick-rayen/pvesync.git netbox-pve-sync && cd netbox-pve-sync
   ./install.sh --netbox-url http://192.168.1.218:8000 --cluster PVE-Production --site inmind.ai
   netbox-pve-sync --dry-run
   systemctl start netbox-pve-sync
   ```
3. Multi-node cluster: install on every node (each gets its own token; one is active). If
   you install it only on some nodes, list them in `proxmox.agent_nodes` on each of them.

Each environment only manages VMs whose `proxmox_source` is its own, so agents never touch
each other's VMs - even with the same NetBox user.

## Upgrading

```bash
cd netbox-pve-sync && git pull && ./install.sh
```

The installer keeps `config.yaml`, `secrets.env` and the state, updates the code, brings the
Proxmox role up to date (e.g. after PVE 8 → 9), re-checks the connection and restarts the
service. After a Proxmox 8 → 9 upgrade always re-run `./install.sh`.

To set the site on an existing installation, run `./install.sh --site inmind.ai` (it adds
`netbox.site` to the kept `config.yaml`) or add `site: "inmind.ai"` under `netbox:` by hand and
restart the service. The startup reconciliation then sets the site and status on every VM.

## Uninstall

```bash
./uninstall.sh                                # remove service and code; keep config, state and token
./uninstall.sh --purge                        # also config, secrets, state, system user, this node's token
./uninstall.sh --purge --remove-pve-access    # also user netbox-sync@pve, role NetBoxSync, ACL (cluster-wide: last node only)
```

NetBox is never changed by uninstalling: synchronized VMs stay. To remove them too, filter
NetBox VMs by tag `tag` and the cluster, and bulk-delete.

## Security considerations

* **Tokens** are never in the source code or in `config.yaml` - only `${NAME}` references.
  `secrets.env` is `0640 root:netbox-pve-sync`. Tokens are masked in logs and CLI output.
  `.env`, `*.secret`, local configs, databases and logs are in `.gitignore`.
* **Proxmox access is read-only**: `VM.Audit`, `Sys.Audit`, and `VM.GuestAgent.Audit` (PVE 9) /
  `VM.Monitor` (PVE 8). On PVE 8, `VM.Monitor` is broader than reading IPs (it also allows the
  QEMU monitor); PVE 8 has no narrower privilege for the guest agent. The `netbox-sync@pve` user
  has no password.
* **NetBox access** is limited to the object types in step 1.2; no admin rights.
* **TLS**: verification is on by default. The installer trusts the Proxmox cluster CA instead of
  disabling verification. `verify_ssl: false` must be set explicitly and makes the connection
  open to man-in-the-middle attacks on that network path.
* **NetBox over plain HTTP** (as `http://192.168.1.218:8000`): the NetBox token travels
  unencrypted over the LAN. Put NetBox behind HTTPS (reverse proxy) when possible and change
  `netbox.url` to `https://`. `check-config` warns about this.
* The service runs unprivileged and sandboxed (see [Logs and systemd](#logs-and-systemd)).

## Development and tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest            # 134 unit/CLI tests, Proxmox and NetBox mocked (no infrastructure needed)
.venv/bin/ruff check src tests
.venv/bin/mypy src
```

Integration tests against a **throwaway** NetBox (never production - they create and delete
objects):

```bash
NETBOX_IT_URL=http://127.0.0.1:18033 NETBOX_IT_TOKEN=<token> .venv/bin/pytest -m integration
```

Project layout:

```
src/netbox_pve_sync/
  config.py          configuration loading/validation, ${VAR} secrets
  proxmox/client.py  Proxmox API client      proxmox/compat.py  PVE 8/9 differences
  proxmox/events.py  event detection         proxmox/vmconfig.py RAM/CPU/disk parsing
  ipselect.py        management IP rules     discovery.py       desired state of a VM
  netbox/client.py   NetBox API client, version compat, dry-run
  netbox/setup.py    custom fields, tag, ownership prerequisites
  sync.py            the sync engine (create/update/delete, IP ordering)
  state.py           SQLite queue and state  service.py         sync, queue, reconciliation
  leader.py          active/standby in clusters  daemon.py      main loop, signals
  cli.py             command line            logging_setup.py   structured logs, secret masking
tests/               unit, CLI and integration tests (fakes.py mimics both APIs)
systemd/  packaging/  install.sh  uninstall.sh
```

See [docs/testing.md](docs/testing.md) for the full validation that was done.

## Known limitations

* **IP changes inside a guest** have no Proxmox event; they are picked up by the local check
  within `local_check_interval` (10 min), not instantly.
* Only **one interface (`eth0`) and one IPv4** per VM are modelled, by design. IPv6 is not synced.
* **LXC containers** are not synced (QEMU VMs only).
* `qm set` on the CLI creates no Proxmox task. Name, CPU, RAM, boot-disk size and status
  changes are still seen within seconds (VM list), but adding/removing a *non-boot* disk this
  way is only noticed by the local check (≤ 10 min). GUI/API changes create tasks and are seen
  within seconds.
* The task-log event source only sees tasks that Proxmox still keeps in `/cluster/tasks`; after
  a long agent outage, startup reconciliation covers anything older.
* NetBox VM name uniqueness is per cluster: two Proxmox VMs with the same name in one
  environment cannot both exist in NetBox; the second is reported as a conflict.
* The agent has been tested against simulated Proxmox APIs and real NetBox 3.3.10 / 4.7.2, and
  installed under real systemd in Debian 12/13 containers - not yet on a physical PVE node.
