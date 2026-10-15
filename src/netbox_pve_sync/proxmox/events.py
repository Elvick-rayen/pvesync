"""Event detection for Proxmox VE 8 and 9.

Proxmox VE has **no push API** for VM lifecycle events (no websocket, SSE or
webhook for "VM created / changed / deleted"; the PVE 8.1+ notification
system only covers backups, replication, fencing and updates; hook scripts
only fire on start/stop of VMs that have one configured). The PVE web UI
itself learns about changes by watching two documented endpoints, and so do we:

``GET /cluster/tasks``
    The cluster-wide task log. Every create, clone, restore, destroy,
    asynchronous config change, disk resize, start and stop is a task with a
    unique ``UPID``. A *new finished task* of a relevant type becomes an event
    for its VMID.

``GET /cluster/resources?type=vm``
    The live VM list with name, configured memory, vCPUs, boot disk size and
    run status. Comparing it with the previous snapshot yields events for VMs
    that appeared (e.g. clone targets, whose UPID names the *source* VM), vanished,
    were renamed, or got new memory/CPU/disk/status - including changes made with
    ``qm set`` on the CLI, which create no task.

Both calls are local to the node (``127.0.0.1:8006``), cheap and read-only,
and NetBox is not involved at all unless one of them reports a change. The
snapshot and the handled UPIDs are stored in SQLite, so changes made while the
agent was stopped are detected right after it starts again.

Events only name a VMID. What to write is always decided by reading the VM's
current state, so duplicated, late or out-of-order events are harmless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..logging_setup import get_logger, log
from ..state import StateStore
from .client import ProxmoxClient, VmResource
from .compat import RELEVANT_TASK_TYPES, START_TASK_TYPES

logger = get_logger("events")


@dataclass(frozen=True)
class VmEvent:
    vmid: int
    kind: str          # "task" | "appeared" | "disappeared" | "changed"
    detail: str
    started: bool = False   # the VM was (re)started: its IP will appear soon


def parse_upid(upid: str) -> dict[str, str]:
    """``UPID:node:pid:pstart:starttime:type:id:user:`` → parts (used when fields are missing)."""
    parts = upid.split(":")
    if len(parts) < 8 or parts[0] != "UPID":
        return {}
    return {"node": parts[1], "type": parts[5], "id": parts[6], "user": parts[7]}


class EventDetector:
    def __init__(self, client: ProxmoxClient, state: StateStore) -> None:
        self.client = client
        self.state = state

    # ------------------------------------------------------------- tasks
    def poll_tasks(self) -> list[VmEvent]:
        tasks = self.client.cluster_tasks()
        seen = self.state.seen_task_ids()
        first_run = self.state.get("tasks_initialized") is None
        new_seen: list[str] = []
        events: list[VmEvent] = []
        for task in tasks:
            upid = str(task.get("upid") or "")
            if not upid or upid in seen:
                continue
            if not task.get("endtime"):
                continue  # still running: handle it once it has finished
            new_seen.append(upid)
            if first_run:
                continue  # history from before the agent existed; startup reconciliation covers it
            info = parse_upid(upid)
            task_type = str(task.get("type") or info.get("type") or "")
            vmid_raw = str(task.get("id") or info.get("id") or "")
            if task_type not in RELEVANT_TASK_TYPES or not vmid_raw.isdigit():
                continue
            status = str(task.get("status") or "")
            events.append(VmEvent(
                vmid=int(vmid_raw), kind="task",
                detail=f"{task_type} ({RELEVANT_TASK_TYPES[task_type]}) status={status or '?'}",
                started=task_type in START_TASK_TYPES and status == "OK",
            ))
        if new_seen:
            self.state.mark_tasks_seen(new_seen)
        if first_run:
            self.state.set("tasks_initialized", "1")
        self.state.prune_tasks()
        return events

    # ------------------------------------------------------------- resources
    def poll_resources(self, resources: list[VmResource]) -> list[VmEvent]:
        previous = self.state.snapshot()
        current = {vm.vmid: vm.change_signature() for vm in resources}
        first_run = self.state.get("snapshot_initialized") is None
        if not current and previous:
            # An empty list while VMs existed before is far more likely a permission or API
            # problem than every VM being deleted at once. Do not turn it into deletions.
            log(logger, logging.WARNING, "Proxmox returned an empty VM list; ignoring it this round "
                "(check the API token's VM.Audit permission)", previous_vms=len(previous))
            return []
        events: list[VmEvent] = []
        if not first_run:
            for vmid, sig in current.items():
                old = previous.get(vmid)
                if old is None:
                    events.append(VmEvent(vmid, "appeared", "VM appeared in the resource list",
                                          started=sig.get("status") == "running"))
                    continue
                changed = sorted(k for k in sig if old.get(k) != sig[k])
                if changed:
                    started = "status" in changed and sig.get("status") == "running"
                    events.append(VmEvent(vmid, "changed", "changed: " + ",".join(changed), started=started))
            for vmid in previous.keys() - current.keys():
                events.append(VmEvent(vmid, "disappeared", "VM no longer in the resource list"))
        if first_run or current != previous:
            self.state.replace_snapshot(current)
        if first_run:
            self.state.set("snapshot_initialized", "1")
        return events

    def poll(self) -> tuple[list[VmEvent], list[VmResource]]:
        """One detection round. Returns the events and the resource list it used."""
        resources = self.client.list_vms()
        events = self.poll_tasks() + self.poll_resources(resources)
        # Collapse several events for the same VM into one (keeping 'started' if any had it).
        merged: dict[int, VmEvent] = {}
        for ev in events:
            prev = merged.get(ev.vmid)
            if prev is None:
                merged[ev.vmid] = ev
            else:
                merged[ev.vmid] = VmEvent(ev.vmid, prev.kind, f"{prev.detail}; {ev.detail}",
                                          started=prev.started or ev.started)
        return list(merged.values()), resources
