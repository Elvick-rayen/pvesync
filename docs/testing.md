# Testing and validation

## 1. Unit and CLI tests (no infrastructure needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q          # 134 passed
.venv/bin/ruff check src tests
.venv/bin/mypy src           # strict, 23 files, no issues
```

`tests/fakes.py` contains in-memory fakes of both APIs. The NetBox fake copies the validation
rules the design depends on (unique names per cluster, no unassigning a primary IP, cascade
deletes); the Proxmox fake rejects any non-GET request, so a test fails if the agent ever writes
to Proxmox.

| Area | Covered in |
|---|---|
| VM create, update, rename, RAM, vCPU, disk | `test_sync.py` |
| IP discovery, selection rules, IPv6/loopback/link-local/Docker rejection | `test_units.py` |
| IP change, interface creation/reuse, management IP, foreign IP conflict | `test_sync.py` |
| VM deletion, IP cleanup policies, ownership checks, other environments untouched | `test_sync.py` |
| Duplicate events, idempotency (4 × sync = 1 VM / 1 eth0 / 1 IP, 0 extra writes) | `test_sync.py`, `test_events_queue.py` |
| NetBox unavailable, retry queue, backoff, restart persistence | `test_events_queue.py` |
| Guest agent unavailable (IP kept), IP wait | `test_sync.py`, `test_events_queue.py` |
| Reconciliation, mass-delete guard, empty VM list guard | `test_events_queue.py` |
| PVE 8 / PVE 9 compatibility layer, NetBox 3 / 4 compatibility | `test_units.py` |
| Leader election, daemon loop, signals | `test_events_queue.py` |
| Config loading/validation, secrets expansion, masking | `test_units.py` |
| CLI: dry-run, sync, reconcile, status, test-connection | `test_cli.py` |

## 2. Integration tests against a real NetBox

`tests/test_integration_netbox.py` (marker `integration`, skipped by default) runs the real
sync engine against a real NetBox REST API with Proxmox faked. **Never point it at production.**

```bash
NETBOX_IT_URL=http://127.0.0.1:18033 \
NETBOX_IT_ADMIN_TOKEN=<admin token for setup> \
NETBOX_IT_TOKEN=<least-privilege agent token> \
.venv/bin/pytest -m integration
```

Results (2026-10-05): **6/6 passed on NetBox 3.3.10** (the version installed at
192.168.1.218) with the least-privilege token from the README, and **6/6 on NetBox 4.7.2**
with a v2 Bearer token. Confirmed for real: cascade delete, primary-IP unassign refusal,
custom-field filters, disk units.

Throwaway NetBox for this: `netboxcommunity/netbox:v3.3.10` + `postgres:16-alpine` +
`redis:7-alpine` in Docker Compose, bound to `127.0.0.1` only.

## 3. Installer and systemd test

`install.sh`, the systemd unit and `uninstall.sh` were run for real in Debian 12 and Debian 13
containers. `pveversion` and `pveum` were stubbed, and the Proxmox API was served by
`tests/fakes.FakeProxmox` over HTTPS with a certificate signed by a test "cluster CA".

**Safety rule for this test:** the systemd container runs **without `--privileged`**, with a
private cgroup namespace, its own minimal `/dev` (no host consoles), only `CAP_SYS_ADMIN`
(needed to remount its *own* cgroup subtree writable for systemd), and all getty/console/logind
units masked. A first attempt with `--privileged` let the container's systemd start a login
prompt on the host's real console; never run systemd containers privileged on a workstation.

Results on Debian 13 + real systemd 257, simulating PVE 9.1.1, against NetBox 3.3.10:

| Check | Result |
|---|---|
| `install.sh --start`: OS/PVE/Python checks, user, files, role/user/token, unit, test-connection | pass |
| File modes: `/etc/netbox-pve-sync` 0750, `config.yaml`/`secrets.env` 0640 root:netbox-pve-sync | pass |
| Service runs as `netbox-pve-sync`, logs in journald | pass |
| Initial reconciliation: 2 VMs with IP, 1 without (agent down), template skipped | pass |
| Live events: RAM change, guest agent starts (IP added), VM destroyed (removed + IP deleted) | pass, within seconds |
| `kill -9` → systemd restart after 15 s → reconcile, nothing changed | pass |
| NetBox stopped → change queued, backoff 5/10/18 s → synced after NetBox returns | pass |
| Restart while NetBox is down → queue survives → synced later | pass |
| `systemctl stop`: clean, 0.1 s, result=success | pass |
| `systemd-analyze security` | 1.2 OK |
| Re-run `install.sh` (upgrade): config/secrets/token kept, role updated, service restarted | pass |
| `sync --vmid` twice → "unchanged - no changes" | pass |
| `uninstall.sh --purge --remove-pve-access`: files, user, token, role, ACL removed; NetBox untouched | pass |
| Tokens in journal or installer output | 0 occurrences |
| `shellcheck` of all shell scripts, `systemd-analyze verify` of the unit | clean |

Earlier run in a plain Debian 12 container (PVE 8.4.1 simulated, apt dependency path) also passed.

## 4. Not yet tested

* A real Proxmox VE 8 or 9 node (real `pveum`, real pveproxy certificate, real guest agents).
  Recommended first deployment: install on one node, `--dry-run`, compare with NetBox, then start.
