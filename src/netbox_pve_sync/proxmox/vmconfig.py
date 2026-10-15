"""Turn a raw Proxmox VM config into the values we synchronize.

All values are *configured allocations*, never runtime usage:

* **Memory** – the ``memory`` option in MiB (PVE default 512 when unset). The
  balloon minimum and current usage are ignored.
* **vCPUs** – ``vcpus`` if set (the number of hot-plugged vCPUs), otherwise
  ``sockets × cores`` (defaults 1 × 1). This is the same number Proxmox shows
  as ``maxcpu`` in the cluster resource list.
* **Disk** – the sum of the ``size=`` of every attached virtual disk
  (``scsiN``, ``virtioN``, ``sataN``, ``ideN``). Not counted: CD-ROM drives
  (``media=cdrom``), detached ``unusedN`` volumes, snapshots, and by default
  the tiny ``efidisk0``/``tpmstate0`` volumes. This is the virtual capacity
  the guest sees – not the space used on the storage, and not the size of
  the storage pool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_DISK_KEY_RE = re.compile(r"^(scsi|virtio|sata|ide)\d+$")
_SPECIAL_DISK_KEYS = ("efidisk0", "tpmstate0")
_NET_KEY_RE = re.compile(r"^net\d+$")
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]?)$", re.IGNORECASE)
_SIZE_FACTORS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
NIC_MODELS = ("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci", "pcnet", "i82551",
              "i82557b", "i82559er", "ne2k_isa", "e1000-82540em", "e1000-82544gc", "e1000-82545em")

DEFAULT_MEMORY_MB = 512


def parse_property_string(value: Any, default_key: str | None = None) -> dict[str, str]:
    """Parse ``"local-lvm:vm-101-disk-0,size=32G,ssd=1"`` into a dict.

    A leading item without ``=`` is stored under ``default_key`` (e.g. the volume).
    """
    result: dict[str, str] = {}
    for index, part in enumerate(str(value).split(",")):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, val = part.split("=", 1)
            result[key.strip()] = val.strip()
        elif index == 0 and default_key:
            result[default_key] = part
    return result


def parse_size(value: str) -> int | None:
    """``"32G"`` → bytes. Proxmox sizes use binary units (G = GiB)."""
    match = _SIZE_RE.match(value.strip())
    if not match:
        return None
    return int(float(match.group(1)) * _SIZE_FACTORS[match.group(2).upper()])


@dataclass(frozen=True)
class DiskInfo:
    key: str
    volume: str
    size_bytes: int | None


@dataclass
class ParsedVmConfig:
    name: str | None
    memory_mb: int
    vcpus: float
    disks: list[DiskInfo]
    agent_enabled: bool
    macs: list[str]  # lower-case, in netN order
    warnings: list[str] = field(default_factory=list)

    def disk_bytes(self) -> int:
        return sum(d.size_bytes or 0 for d in self.disks)


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_memory(value: Any) -> int:
    """``4096`` or ``current=4096`` (newer qemu-server property string) → MiB."""
    if value is None or value == "":
        return DEFAULT_MEMORY_MB
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    props = parse_property_string(value, default_key="current")
    return _parse_int(props.get("current"), DEFAULT_MEMORY_MB)


def parse_vcpus(config: dict[str, Any]) -> float:
    if config.get("vcpus") not in (None, "", 0, "0"):
        return float(_parse_int(config["vcpus"], 1))
    sockets = max(1, _parse_int(config.get("sockets", 1), 1))
    cores = max(1, _parse_int(config.get("cores", 1), 1))
    return float(sockets * cores)


def parse_agent_enabled(value: Any) -> bool:
    if value is None or value == "":
        return False
    props = parse_property_string(value, default_key="enabled")
    return props.get("enabled", "0").strip().lower() in ("1", "yes", "true", "on")


def parse_mac(value: Any) -> str | None:
    props = parse_property_string(value)
    for model in NIC_MODELS:
        mac = props.get(model)
        if mac and _MAC_RE.match(mac):
            return mac.lower()
    mac = props.get("macaddr")
    if mac and _MAC_RE.match(mac):
        return mac.lower()
    # Unknown NIC model: take the first key=value that looks like a MAC.
    for val in props.values():
        if _MAC_RE.match(val):
            return val.lower()
    return None


def _key_order(key: str) -> tuple[str, int]:
    match = re.match(r"^([a-z]+)(\d+)$", key)
    return (match.group(1), int(match.group(2))) if match else (key, 0)


def parse_vm_config(config: dict[str, Any], *, include_efi_tpm_disks: bool = False) -> ParsedVmConfig:
    warnings: list[str] = []
    disks: list[DiskInfo] = []
    disk_keys = [k for k in config if _DISK_KEY_RE.match(k)]
    if include_efi_tpm_disks:
        disk_keys += [k for k in _SPECIAL_DISK_KEYS if k in config]
    for key in sorted(disk_keys, key=_key_order):
        props = parse_property_string(config[key], default_key="volume")
        volume = props.get("volume", "")
        if props.get("media") == "cdrom" or volume in ("none", "cdrom"):
            continue
        size = parse_size(props["size"]) if "size" in props else None
        if size is None:
            warnings.append(f"disk {key} has no readable size; it is not counted")
        disks.append(DiskInfo(key=key, volume=volume, size_bytes=size))

    net_keys = sorted((k for k in config if _NET_KEY_RE.match(k)), key=_key_order)
    macs = [m for m in (parse_mac(config[k]) for k in net_keys) if m]

    name = config.get("name")
    return ParsedVmConfig(
        name=str(name).strip() if name else None,
        memory_mb=parse_memory(config.get("memory")),
        vcpus=parse_vcpus(config),
        disks=disks,
        agent_enabled=parse_agent_enabled(config.get("agent")),
        macs=macs,
        warnings=warnings,
    )
