# Architecture and design notes

This file records *why* the agent works the way it does, including the API questions that
were investigated before implementing. User-facing documentation is in the [README](../README.md).

## 1. Event source: what Proxmox VE 8 and 9 actually offer

Investigated options:

| Mechanism | Usable for VM create/change/delete? | Conclusion |
|---|---|---|
| Websocket / SSE / webhook for VM lifecycle | does not exist in PVE 8 or 9 | - |
| Notification system (PVE 8.1+, webhooks in 8.3+) | only backup, replication, fencing, package updates | not suitable |
| Hook scripts (`hookscript=`) | only pre/post start/stop, per VM, must be configured on each VM | not suitable |
| `pmxcfs` file watching (`/etc/pve/qemu-server/*.conf`) | works, but undocumented internal behaviour and needs root | rejected (the requirement prefers documented APIs and least privilege) |
| **`GET /cluster/tasks`** | every create, clone, restore, destroy, async config change, resize, migrate, start, stop is a task with a UPID | **used** |
| **`GET /cluster/resources?type=vm`** | live VM list: name, maxmem, maxcpu, boot-disk maxdisk, status, template, tags | **used** |

The Proxmox web UI itself refreshes from these two endpoints. Both exist with the same shape
on PVE 8 and 9, are cluster-wide, local (`127.0.0.1:8006`) and need only `Sys.Audit`/`VM.Audit`.

Polling them every 5 seconds is *local change detection*, not synchronization polling:
NetBox is not contacted unless one of them shows a relevant change. The 10-minute *local check*
re-reads all VM configs and guest-agent data from Proxmox only, compares a fingerprint with the
last synced state, and contacts NetBox only on a difference. It exists because guest IP changes
generate no Proxmox event at all.

Event handling details ([proxmox/events.py](../src/netbox_pve_sync/proxmox/events.py)):

* A task becomes an event once it has *finished* (`endtime` set). Handled UPIDs are stored, so
  each task triggers once - also across restarts.
* On first start the existing task history is only marked as seen; the startup reconciliation
  covers the current state.
* `qmclone` names the *source* VMID; the new VM is detected because it appears in the resource list.
* `qm set` on the CLI creates no task; the resource-list diff catches name/CPU/RAM/boot
  disk/status changes; other disks are caught by the local check.
* An empty VM list while VMs existed before is ignored (permission/API problem, not mass deletion).
* Several events for one VM in one round are merged.

## 2. PVE 8 vs PVE 9 differences ([proxmox/compat.py](../src/netbox_pve_sync/proxmox/compat.py))

| Topic | PVE 8 | PVE 9 |
|---|---|---|
| Privilege for `agent/network-get-interfaces` | `VM.Monitor` | `VM.Monitor` removed; `VM.GuestAgent.Audit` (read-only) |
| `memory` config value | `4096` | `4096` or property string `current=4096` (newer qemu-server) |
| Python | 3.11 (Debian 12) | 3.13 (Debian 13) |

Everything else used (`/version`, `/cluster/status`, `/cluster/tasks`, `/cluster/resources`,
`/nodes/{node}/qemu/{vmid}/config`, `/access/permissions`) is unchanged. The version is read from
`GET /version` at startup and logged. `install.sh` selects the right privilege and updates the
role when re-run after a PVE 8 → 9 upgrade (`pve8to9` warns about exactly this).

## 3. NetBox version differences ([netbox/client.py](../src/netbox_pve_sync/netbox/client.py))

| Topic | NetBox 3.x | NetBox 4.x |
|---|---|---|
| `VirtualMachine.disk` unit | GB | MB (since 4.0) |
| Custom field object types key | `content_types` | `object_types` |
| Token header | `Token <40 hex>` | `Token` (v1) or `Bearer nbt_<key>.<secret>` (v2, 4.5+) |
| `VirtualMachine.virtual_disk_count` | - | present; if > 0 NetBox computes `disk` itself |

The version comes from `GET /api/status/` and is logged. Minimum 3.3; newer majors than 4 log a
warning. The user's NetBox reported **3.3.10**. Both 3.3.10 and 4.7.2 were tested for real (see
[testing.md](testing.md)), including: cascade delete of interfaces and IPs with a VM, refusal to
unassign an IP that is still primary, `cf_proxmox_vmid` filtering, case-insensitive name uniqueness
per cluster, and a least-privilege agent user.

Disk unit: Proxmox sizes are binary (`G` = GiB). NetBox 3.x: value = bytes / 1024³ (100G → 100).
NetBox 4.x: value = bytes / 1024² (100G → 102400), which NetBox 4 displays as 100 GB.

## 4. Data model and identity

* **Identity**: custom field `proxmox_vmid` (integer). Names can change; VMIDs cannot.
* **Ownership**: cluster + tag `netbox-pve-sync` + `proxmox_source == sync.source_id` +
  `proxmox_vmid` set. All four must match before anything is changed or deleted.
  `source_id` defaults to the cluster name, so two environments never collide.
* **Created IPs** carry the same tag; that is how `ip_cleanup_policy: delete_managed`
  recognizes IPs it may delete.
* **Duplicates**: VM lookup is by `cluster_id` + `cf_proxmox_vmid`. If two owned VMs share a VMID
  (crash between create and state write), the oldest is kept, the other deleted.

## 5. Sync flow ([sync.py](../src/netbox_pve_sync/sync.py))

```
discover (Proxmox, read-only)          sync (NetBox)
  config  -> name, RAM, vCPU, disk       find owned VM by VMID  -> none? name free? create : conflict
  agent   -> interfaces -> ipselect      update only differing fields
                                          ensure eth0
  no usable IP? -> keep NetBox IP         IP: on eth0? / exists in IPAM (mine | unassigned | foreign)
                                              / create assigned to eth0
                                          set primary_ip4
                                          then remove the previous IP (policy)
```

Deletion: clear primary IP (only if IPs must survive), unassign IPs to keep, delete tagged IPs
(policy), delete VM (NetBox cascades interfaces).

## 6. Failure handling

| Failure | Behaviour |
|---|---|
| NetBox unreachable / 5xx / timeout | item stays in SQLite queue, backoff 5 s × 2 … 1 h, ±10 % jitter; reconciliation postponed |
| NetBox 401/403 | logged as error; at startup: exit 78 (no restart loop) |
| Proxmox unreachable | event polling backs off (≤ 5 min), startup retries |
| VM vanished during sync | treated as deletion |
| Guest agent unavailable / malformed data | VM synced without IP change; IP-wait schedule 30 s … 15 min for 24 h |
| Conflicts (name, foreign IP) | logged with the holder; retried slowly from 1 min |
| One VM fails | other VMs continue; errors are per VM |
| Unexpected exception in the loop | logged, loop continues after 30 s |
| Crash | systemd restarts after 15 s; queue and snapshot are persistent |

## 7. Multi-node clusters ([leader.py](../src/netbox_pve_sync/leader.py))

Every node sees all VMs of its cluster. Rule without locks or extra services: the online node
with the lowest corosync node ID (or the first online node in `agent_nodes`) is active, checked
every 30 s from `GET /cluster/status`. On takeover the new active node reconciles at once. A
short double-active window during a partition is harmless because all writes are idempotent
and ownership-checked.

## 8. Why no Redis/RabbitMQ/PostgreSQL

The agent is one process per Proxmox environment with a workload of a few API calls per change.
SQLite in WAL mode (stdlib, no service) gives a durable queue that survives reboots. NetBox stays
the inventory; the SQLite file is a cache and can be deleted.
