"""Build the *desired* NetBox state of a VM from Proxmox (the source of truth)."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from .config import Config
from .errors import NotFoundError
from .ipselect import IpCandidate, IpSelection, select_management_ipv4
from .logging_setup import get_logger, log
from .proxmox.client import GuestAgentUnavailable, ProxmoxClient, VmResource
from .proxmox.vmconfig import parse_vm_config

logger = get_logger("discovery")

# Why there is (or is not) a usable management IP.
IP_OK = "ok"
IP_VM_STOPPED = "vm_stopped"
IP_AGENT_DISABLED = "agent_disabled"
IP_AGENT_NOT_RUNNING = "agent_not_running"
IP_AGENT_ERROR = "agent_error"
IP_NONE_USABLE = "no_usable_ip"

# Proxmox runtime status -> NetBox VM status. Anything else (e.g. "unknown" while
# the VM's node is offline) leaves the NetBox status as it is.
NETBOX_STATUS = {"running": "active", "stopped": "offline"}


@dataclass
class DesiredVM:
    vmid: int
    name: str
    memory_mb: int
    vcpus: float
    disk_bytes: int
    status: str                 # Proxmox runtime status (synced as NetBox status; also used for IP decisions)
    node: str                   # needed for API calls only; never written to NetBox
    ip: IpCandidate | None
    ip_status: str
    ip_detail: str = ""
    selection: IpSelection | None = None
    warnings: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Hash of everything we write to NetBox. Same hash = nothing to do."""
        data = {"name": self.name, "memory": self.memory_mb, "vcpus": self.vcpus,
                "disk": self.disk_bytes, "ip": self.ip.cidr if self.ip else None, "status": self.netbox_status}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    @property
    def netbox_status(self) -> str | None:
        """NetBox status for the Proxmox power state: running -> active, stopped -> offline."""
        return NETBOX_STATUS.get(self.status)

    @property
    def waiting_for_ip(self) -> bool:
        """True when an IP is expected soon (VM running with agent enabled, but no address yet)."""
        return self.status == "running" and self.ip_status in (IP_AGENT_NOT_RUNNING, IP_AGENT_ERROR, IP_NONE_USABLE)


def is_eligible(vm: VmResource, config: Config) -> tuple[bool, str]:
    """Whether a Proxmox VM should exist in NetBox, and if not, why."""
    if vm.template and not config.sync.include_templates:
        return False, "template"
    if vm.vmid in config.sync.exclude_vmids:
        return False, "vmid excluded in config"
    excluded = set(config.sync.exclude_pve_tags) & set(vm.tags)
    if excluded:
        return False, f"Proxmox tag {sorted(excluded)[0]} excluded in config"
    return True, ""


def default_vm_name(vmid: int) -> str:
    return f"vm-{vmid}"


def discover_vm(client: ProxmoxClient, resource: VmResource, config: Config) -> DesiredVM:
    """Read config and guest-agent data of one VM. Raises NotFoundError if it vanished meanwhile."""
    raw = client.vm_config(resource.node, resource.vmid)
    parsed = parse_vm_config(raw, include_efi_tpm_disks=config.sync.include_efi_tpm_disks)
    desired = DesiredVM(
        vmid=resource.vmid,
        name=parsed.name or resource.name or default_vm_name(resource.vmid),
        memory_mb=parsed.memory_mb,
        vcpus=parsed.vcpus,
        disk_bytes=parsed.disk_bytes(),
        status=resource.status,
        node=resource.node,
        ip=None,
        ip_status=IP_NONE_USABLE,
        warnings=list(parsed.warnings),
    )
    if not parsed.agent_enabled:
        desired.ip_status = IP_AGENT_DISABLED
        desired.ip_detail = "QEMU guest agent is not enabled in the VM options (agent: 1)"
        return desired
    if resource.status != "running":
        desired.ip_status = IP_VM_STOPPED
        desired.ip_detail = f"VM is {resource.status}"
        return desired
    try:
        interfaces = client.guest_network_interfaces(resource.node, resource.vmid)
    except GuestAgentUnavailable as exc:
        desired.ip_status = IP_AGENT_DISABLED if exc.reason == "not_configured" else (
            IP_AGENT_NOT_RUNNING if exc.reason in ("not_running", "timeout") else IP_AGENT_ERROR)
        desired.ip_detail = str(exc)
        return desired
    except NotFoundError:
        raise
    selection = select_management_ipv4(interfaces, parsed.macs, config.ip_selection)
    desired.selection = selection
    if selection.malformed:
        log(logger, logging.WARNING, "Guest agent returned partly malformed network data",
            vmid=resource.vmid)
    if selection.selected:
        desired.ip = selection.selected
        desired.ip_status = IP_OK
    else:
        desired.ip_status = IP_NONE_USABLE
        desired.ip_detail = "guest agent reported no usable IPv4 address"
    return desired
