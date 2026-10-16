"""The synchronization engine: make NetBox match one Proxmox VM.

Design rules
------------
* **Level-triggered, not edge-triggered.** Every sync reads the VM's *current*
  state from Proxmox and makes NetBox match it. Events only say *which* VM to
  look at. Duplicate or out-of-order events therefore cannot do harm.
* **Read → compare → write only the difference.** Running a sync four times
  gives one VM, one ``eth0`` and one IP; the last three runs write nothing.
* **Ownership first.** Only VMs owned by this agent (see :mod:`netbox.setup`)
  are ever changed or deleted.
* **Never lose a known IP because the guest agent is down.** When there is no
  usable IP right now, the IP part is skipped and NetBox keeps what it has.
* **Never steal an IP.** An IP object assigned to anything other than this VM
  is left alone and reported as a conflict.

NetBox IP operations are ordered so NetBox validation never rejects them:

1. ensure the VM exists, 2. ensure ``eth0`` exists, 3. create the IP already
assigned to ``eth0`` (or assign the existing one), 4. set ``primary_ip4`` (the
"Management IPv4"), 5. only then detach/delete the previous IP – NetBox refuses
to reassign an IP that is still the primary IP of its parent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .discovery import DesiredVM
from .errors import ConflictError, ValidationError
from .logging_setup import get_logger, log
from .netbox.client import EP_IPS, EP_VM_INTERFACES, EP_VMS, VMINTERFACE_TYPE, NetBoxClient, obj_id
from .netbox.setup import CF_SOURCE, CF_VMID, NetBoxPrereqs

logger = get_logger("sync")

MANAGED_DESCRIPTION = "Managed by netbox-pve-sync"


@dataclass
class SyncResult:
    vmid: int
    outcome: str                       # created | updated | unchanged | deleted | absent | conflict
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflict: bool = False             # something needs a human; retry slowly
    netbox_id: int | None = None
    ip_status: str = ""


def _tag_ids(obj: dict[str, Any]) -> list[int]:
    return [int(t["id"]) if isinstance(t, dict) else int(t) for t in obj.get("tags") or []]


def _same_number(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def _choice(value: Any) -> Any:
    """NetBox returns choice fields such as ``status`` as ``{"value": ..., "label": ...}``."""
    return value.get("value") if isinstance(value, dict) else value


def _host(address: str) -> str:
    return str(address).split("/")[0]


class Synchronizer:
    def __init__(self, netbox: NetBoxClient, config: Config, prereqs: NetBoxPrereqs) -> None:
        self.nb = netbox
        self.config = config
        self.prereqs = prereqs
        self.source = config.sync.source_id
        self.ifname = config.sync.interface_name

    # ================================================================ ownership
    def is_owned(self, vm: dict[str, Any]) -> bool:
        cf = vm.get("custom_fields") or {}
        return (obj_id(vm.get("cluster")) == self.prereqs.cluster_id
                and self.prereqs.tag_id in _tag_ids(vm)
                and cf.get(CF_SOURCE) == self.source
                and cf.get(CF_VMID) is not None)

    def managed_vms(self) -> dict[int, list[dict[str, Any]]]:
        """All NetBox VMs owned by this agent, grouped by Proxmox VMID."""
        result: dict[int, list[dict[str, Any]]] = {}
        for vm in self.nb.vms_in_cluster(self.prereqs.cluster_id, tag=self.config.netbox.tag):
            if self.is_owned(vm):
                result.setdefault(int(vm["custom_fields"][CF_VMID]), []).append(vm)
        return result

    def find_owned(self, vmid: int) -> list[dict[str, Any]]:
        found = self.nb.vms_in_cluster(self.prereqs.cluster_id, **{f"cf_{CF_VMID}": vmid})
        return [vm for vm in found if self.is_owned(vm) and int(vm["custom_fields"][CF_VMID]) == vmid]

    def _resolve_duplicates(self, vmid: int, vms: list[dict[str, Any]], result: SyncResult) -> dict[str, Any] | None:
        """Several owned NetBox VMs for one VMID (e.g. after a crash mid-create): keep the oldest."""
        if not vms:
            return None
        vms = sorted(vms, key=lambda v: int(v["id"]))
        for extra in vms[1:]:
            log(logger, logging.WARNING, "Removing duplicate NetBox VM", vmid=vmid, netbox_id=extra["id"],
                kept_id=vms[0]["id"])
            self._delete_vm_object(extra, result)
            result.changes.append(f"deleted duplicate NetBox VM #{extra['id']}")
        return vms[0]

    # ================================================================ public API
    def sync(self, desired: DesiredVM, existing: list[dict[str, Any]] | None = None) -> SyncResult:
        """Create or update the NetBox VM for ``desired``. ``existing`` may be passed by the reconciler."""
        result = SyncResult(vmid=desired.vmid, outcome="unchanged", ip_status=desired.ip_status,
                            warnings=list(desired.warnings))
        owned = self.find_owned(desired.vmid) if existing is None else existing
        vm = self._resolve_duplicates(desired.vmid, owned, result)

        if vm is None:
            vm = self._adopt_or_create(desired, result)
            if vm is None:
                return result
        else:
            self._update_vm(vm, desired, result)
        result.netbox_id = int(vm["id"])

        iface = self._ensure_interface(vm, result)
        if desired.ip is None:
            log(logger, logging.DEBUG, "No usable IP now; keeping the NetBox IP unchanged", vmid=desired.vmid,
                reason=desired.ip_status)
        else:
            self._sync_ip(vm, iface, desired, result)

        if result.outcome == "unchanged" and result.changes:
            result.outcome = "updated"
        if result.conflict and result.outcome == "unchanged":
            result.outcome = "conflict"
        return result

    def delete(self, vmid: int, existing: list[dict[str, Any]] | None = None) -> SyncResult:
        """Delete the owned NetBox VM(s) for a Proxmox VM that no longer exists."""
        result = SyncResult(vmid=vmid, outcome="absent")
        owned = self.find_owned(vmid) if existing is None else existing
        for vm in owned:
            if not self.is_owned(vm):  # defence in depth: never delete what we do not own
                continue
            self._delete_vm_object(vm, result)
            result.outcome = "deleted"
            result.changes.append(f"deleted NetBox VM {vm.get('name')} (#{vm['id']})")
        return result

    # ================================================================ VM object
    def _desired_fields(self, desired: DesiredVM) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "name": desired.name,
            "cluster": self.prereqs.cluster_id,
            "vcpus": desired.vcpus,
            "memory": desired.memory_mb,
            "disk": self.nb.compat.disk_value(desired.disk_bytes),
        }
        if self.prereqs.site_id is not None:
            fields["site"] = self.prereqs.site_id
        if self.config.sync.sync_status and desired.netbox_status is not None:
            fields["status"] = desired.netbox_status
        return fields

    def _name_taken(self, name: str, except_id: int | None) -> dict[str, Any] | None:
        # NetBox enforces case-insensitive unique VM names per cluster (and tenant).
        for vm in self.nb.vms_in_cluster(self.prereqs.cluster_id, name__ie=name):
            if str(vm.get("name", "")).lower() == name.lower() and vm.get("id") != except_id:
                return dict(vm)
        return None

    def _adopt_or_create(self, desired: DesiredVM, result: SyncResult) -> dict[str, Any] | None:
        clash = self._name_taken(desired.name, None)
        if clash is not None:
            cf = clash.get("custom_fields") or {}
            foreign = cf.get(CF_VMID) is not None or (cf.get(CF_SOURCE) not in (None, "", self.source))
            if self.config.sync.adopt_existing and not foreign:
                log(logger, logging.INFO, "Adopting existing NetBox VM with the same name", vmid=desired.vmid,
                    name=desired.name, netbox_id=clash["id"])
                result.changes.append(f"adopted existing NetBox VM #{clash['id']}")
                self._update_vm(clash, desired, result, adopting=True)
                result.outcome = "updated"
                return clash
            why = ("it belongs to another Proxmox VM/source" if foreign else
                   "it is not managed by netbox-pve-sync (set sync.adopt_existing: true to take it over)")
            msg = f"NetBox VM '{desired.name}' already exists in cluster {self.prereqs.cluster_name} and {why}"
            log(logger, logging.ERROR, "Cannot create VM in NetBox: name conflict", vmid=desired.vmid,
                name=desired.name, netbox_id=clash["id"], detail=msg)
            result.warnings.append(msg)
            result.conflict = True
            result.outcome = "conflict"
            return None

        data = self._desired_fields(desired)
        data.setdefault("status", "active")
        data.update({
            "tags": [self.prereqs.tag_id],
            "custom_fields": {CF_VMID: desired.vmid, CF_SOURCE: self.source},
        })
        vm = self.nb.create(EP_VMS, data, describe=f"VM {desired.name} (vmid {desired.vmid})")
        result.outcome = "created"
        result.changes.append("created VM")
        return vm

    def _update_vm(self, vm: dict[str, Any], desired: DesiredVM, result: SyncResult, *,
                   adopting: bool = False) -> None:
        patch: dict[str, Any] = {}
        want = self._desired_fields(desired)
        if vm.get("name") != want["name"]:
            if self._name_taken(want["name"], int(vm["id"])) is not None:
                msg = f"cannot rename to '{want['name']}': another NetBox VM in the cluster has that name"
                log(logger, logging.ERROR, "Rename conflict", vmid=desired.vmid, detail=msg)
                result.warnings.append(msg)
                result.conflict = True
            else:
                patch["name"] = want["name"]
                result.changes.append(f"name {vm.get('name')} -> {want['name']}")
        if obj_id(vm.get("cluster")) != want["cluster"]:
            patch["cluster"] = want["cluster"]
            result.changes.append("cluster")
        if "site" in want and obj_id(vm.get("site")) != want["site"]:
            patch["site"] = want["site"]
            result.changes.append(f"site -> {self.config.netbox.site}")
        if "status" in want and _choice(vm.get("status")) != want["status"]:
            patch["status"] = want["status"]
            result.changes.append(f"status {_choice(vm.get('status'))} -> {want['status']}")
        if not _same_number(vm.get("vcpus"), want["vcpus"]):
            patch["vcpus"] = want["vcpus"]
            result.changes.append(f"vcpus {vm.get('vcpus')} -> {want['vcpus']:g}")
        if not _same_number(vm.get("memory"), want["memory"]):
            patch["memory"] = want["memory"]
            result.changes.append(f"memory {vm.get('memory')} -> {want['memory']} MB")
        if not _same_number(vm.get("disk"), want["disk"]):
            if int(vm.get("virtual_disk_count") or 0) > 0:
                # NetBox >= 4.0 computes VM.disk from VirtualDisk objects someone created by hand.
                result.warnings.append("disk not updated: the NetBox VM has virtual disk objects")
            else:
                patch["disk"] = want["disk"]
                result.changes.append(f"disk {vm.get('disk')} -> {want['disk']} {self.nb.compat.disk_unit_name}")
        tags = _tag_ids(vm)
        if self.prereqs.tag_id not in tags:
            patch["tags"] = [*tags, self.prereqs.tag_id]  # PATCH replaces the list: keep existing tags
            result.changes.append("tag")
        cf = vm.get("custom_fields") or {}
        if cf.get(CF_VMID) != desired.vmid or cf.get(CF_SOURCE) != self.source:
            patch["custom_fields"] = {CF_VMID: desired.vmid, CF_SOURCE: self.source}
            if not adopting:
                result.changes.append("ownership fields")
        if patch:
            self.nb.update(EP_VMS, int(vm["id"]), patch, describe=f"VM {desired.name} (vmid {desired.vmid})")
            vm.update(patch)

    def _delete_vm_object(self, vm: dict[str, Any], result: SyncResult) -> None:
        """Delete a VM, handling IPs first.

        NetBox cascades: deleting a VM deletes its interfaces, and deleting an
        interface deletes the IP objects assigned to it. So *before* deleting,
        every IP we want to keep is detached (after clearing ``primary_ip4``,
        because NetBox refuses to detach a primary IP).
        """
        vm_id = int(vm["id"])
        to_keep: list[dict[str, Any]] = []
        to_delete: list[dict[str, Any]] = []
        for iface in self.nb.vm_interfaces(vm_id):
            for ip in self.nb.ips_on_interface(int(iface["id"])):
                if self._ip_deletable(ip):
                    to_delete.append(ip)
                else:
                    to_keep.append(ip)
        if to_keep and (vm.get("primary_ip4") or vm.get("primary_ip6")):
            self.nb.update(EP_VMS, vm_id, {"primary_ip4": None, "primary_ip6": None},
                           describe=f"VM {vm.get('name')}: clear primary IP before deletion")
        for ip in to_keep:
            self._unassign_ip(ip, result, reason="VM deleted")
        for ip in to_delete:
            self.nb.delete(EP_IPS, int(ip["id"]), describe=f"IP {ip.get('address')} (created by netbox-pve-sync)")
            result.changes.append(f"deleted IP {ip.get('address')}")
        self.nb.delete(EP_VMS, vm_id, describe=f"VM {vm.get('name')} (#{vm_id})")

    # ================================================================ interface
    def _ensure_interface(self, vm: dict[str, Any], result: SyncResult) -> dict[str, Any]:
        vm_id = int(vm["id"])
        for iface in self.nb.vm_interfaces(vm_id):
            if iface.get("name") == self.ifname:
                return iface
        data = {"virtual_machine": vm_id, "name": self.ifname, "enabled": True, "tags": [self.prereqs.tag_id]}
        try:
            iface = self.nb.create(EP_VM_INTERFACES, data, describe=f"interface {self.ifname} on VM {vm.get('name')}")
        except ValidationError:
            # Lost a race with another writer: the interface exists now, use it.
            for iface in self.nb.vm_interfaces(vm_id):
                if iface.get("name") == self.ifname:
                    return iface
            raise
        result.changes.append(f"created interface {self.ifname}")
        return iface

    # ================================================================ IP address
    def _ip_deletable(self, ip: dict[str, Any]) -> bool:
        """Only IPs this agent created (tagged) may be deleted, and only under policy delete_managed."""
        return (self.config.sync.ip_cleanup_policy == "delete_managed"
                and self.prereqs.tag_id in _tag_ids(ip))

    def _unassign_ip(self, ip: dict[str, Any], result: SyncResult, *, reason: str) -> None:
        self.nb.update(EP_IPS, int(ip["id"]), {"assigned_object_type": None, "assigned_object_id": None},
                       describe=f"IP {ip.get('address')}: unassign ({reason})")
        result.changes.append(f"unassigned IP {ip.get('address')}")

    def _belongs_to_vm(self, ip: dict[str, Any], vm_id: int) -> bool:
        if ip.get("assigned_object_type") != VMINTERFACE_TYPE:
            return False
        assigned = ip.get("assigned_object") or {}
        return obj_id(assigned.get("virtual_machine")) == vm_id

    def _find_or_create_ip(self, vm: dict[str, Any], iface: dict[str, Any], desired: DesiredVM,
                           eth0_ips: list[dict[str, Any]], result: SyncResult) -> dict[str, Any] | None:
        assert desired.ip is not None
        cidr = desired.ip.cidr
        iface_id = int(iface["id"])
        vm_id = int(vm["id"])

        # 1. Already on eth0?
        for ip in eth0_ips:
            if _host(ip["address"]) == desired.ip.address:
                if ip["address"] != cidr and self.prereqs.tag_id in _tag_ids(ip):
                    self.nb.update(EP_IPS, int(ip["id"]), {"address": cidr},
                                   describe=f"IP {ip['address']} -> {cidr}")
                    result.changes.append(f"ip prefix {ip['address']} -> {cidr}")
                    ip = {**ip, "address": cidr}
                return ip

        # 2. Exists somewhere in IPAM?
        existing = self.nb.ips_by_address(cidr, self.prereqs.vrf_id)
        mine = [ip for ip in existing if self._belongs_to_vm(ip, vm_id)]
        free = [ip for ip in existing if not ip.get("assigned_object_type")]
        if mine or (free and self.config.sync.claim_unassigned_ips):
            ip = sorted(mine or free, key=lambda i: int(i["id"]))[0]
            self.nb.update(EP_IPS, int(ip["id"]),
                           {"assigned_object_type": VMINTERFACE_TYPE, "assigned_object_id": iface_id},
                           describe=f"IP {ip['address']}: assign to {self.ifname}")
            result.changes.append(f"assigned existing IP {ip['address']} to {self.ifname}")
            return {**ip, "assigned_object_type": VMINTERFACE_TYPE, "assigned_object_id": iface_id}
        if existing:
            holder = existing[0]
            owner = holder.get("assigned_object") or {}
            parent = owner.get("virtual_machine") or owner.get("device") or {}
            where = f"{parent.get('name', '?')} / {owner.get('name', '?')}" if owner else "unassigned"
            msg = (f"IP {cidr} already exists in NetBox and is assigned to {where}; "
                   "not taking it over (fix the IP in NetBox or in the guest)")
            log(logger, logging.WARNING, "IP conflict: address belongs to another object", vmid=desired.vmid,
                ip=cidr, holder=where, netbox_ip_id=holder.get("id"))
            result.warnings.append(msg)
            result.conflict = True
            return None

        # 3. Brand new: create it already assigned to eth0 and tagged as ours.
        data: dict[str, Any] = {
            "address": cidr, "status": "active", "description": MANAGED_DESCRIPTION,
            "assigned_object_type": VMINTERFACE_TYPE, "assigned_object_id": iface_id,
            "tags": [self.prereqs.tag_id],
        }
        if self.prereqs.vrf_id is not None:
            data["vrf"] = self.prereqs.vrf_id
        ip = self.nb.create(EP_IPS, data, describe=f"IP {cidr} on {self.ifname} of VM {desired.name}")
        result.changes.append(f"created IP {cidr}")
        return ip

    def _sync_ip(self, vm: dict[str, Any], iface: dict[str, Any], desired: DesiredVM, result: SyncResult) -> None:
        assert desired.ip is not None
        eth0_ips = self.nb.ips_on_interface(int(iface["id"]))
        ip = self._find_or_create_ip(vm, iface, desired, eth0_ips, result)
        if ip is None:
            return
        old_primary = obj_id(vm.get("primary_ip4"))
        if old_primary != int(ip["id"]):
            self.nb.update(EP_VMS, int(vm["id"]), {"primary_ip4": int(ip["id"])},
                           describe=f"VM {desired.name}: primary IPv4 {desired.ip.cidr}")
            result.changes.append(f"primary_ip4 -> {desired.ip.cidr}")
            vm["primary_ip4"] = {"id": int(ip["id"]), "address": desired.ip.cidr}

        # Remove the *previous* management IP from eth0: the old primary, or any IP we created.
        for stale in eth0_ips:
            if int(stale["id"]) == int(ip["id"]) or ":" in _host(stale["address"]):
                continue
            ours = self.prereqs.tag_id in _tag_ids(stale)
            if int(stale["id"]) != old_primary and not ours:
                continue  # an extra IP someone added by hand: leave it alone
            if self._ip_deletable(stale):
                self.nb.delete(EP_IPS, int(stale["id"]), describe=f"old IP {stale['address']}")
                result.changes.append(f"deleted old IP {stale['address']}")
            else:
                self._unassign_ip(stale, result, reason="IP changed")


def raise_if_conflict(result: SyncResult) -> None:
    if result.conflict:
        raise ConflictError("; ".join(result.warnings) or "conflict")
