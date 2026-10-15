"""Proxmox VE 8 / 9 compatibility layer.

Everything this agent uses is part of the documented, stable ``/api2/json``
API and behaves the same on PVE 8 and PVE 9, with these verified exceptions:

* **Guest agent privilege.** PVE 8 guards ``/nodes/{node}/qemu/{vmid}/agent/*``
  with ``VM.Monitor``. PVE 9 removed ``VM.Monitor`` and split it into
  ``VM.GuestAgent.*`` privileges; the read-only ``network-get-interfaces``
  command needs ``VM.GuestAgent.Audit``. A role created on PVE 8 with
  ``VM.Monitor`` therefore stops working for IP discovery after an upgrade
  to PVE 9 (``pve8to9`` warns about it; ``install.sh`` fixes it).
* **Memory option format.** ``memory`` may be a plain integer (``4096``) or a
  property string (``current=4096``) on newer qemu-server versions. The
  parser in :mod:`vmconfig` accepts both on both versions.

Neither version offers a push/event-stream API for VM lifecycle events (the
notification system only covers backups, replication, fencing and package
updates). Both versions expose the same cluster task log and resource list,
which is what the PVE web UI itself watches; :mod:`events` builds events from
those.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import FatalStartupError

MIN_SUPPORTED_MAJOR = 8
MAX_TESTED_MAJOR = 9


@dataclass(frozen=True)
class PveVersion:
    version: str  # e.g. "9.1.1"
    release: str  # e.g. "9.1"
    repoid: str = ""

    @property
    def major(self) -> int:
        try:
            return int(self.version.split(".")[0])
        except (ValueError, IndexError):
            return 0

    @classmethod
    def from_api(cls, data: dict[str, object]) -> PveVersion:
        version = str(data.get("version") or "")
        return cls(version=version, release=str(data.get("release") or version), repoid=str(data.get("repoid") or ""))


@dataclass(frozen=True)
class PveCompat:
    """Version-specific behaviour, selected once at startup from ``GET /version``."""

    version: PveVersion

    @property
    def major(self) -> int:
        return self.version.major

    @property
    def guest_agent_privileges(self) -> tuple[str, ...]:
        """Privileges that allow ``agent/network-get-interfaces`` (any one of them is enough)."""
        if self.major >= 9:
            return ("VM.GuestAgent.Audit", "VM.GuestAgent.Unrestricted")
        return ("VM.Monitor",)

    @property
    def required_vm_privileges(self) -> tuple[str, ...]:
        return ("VM.Audit",)

    @property
    def role_privileges(self) -> tuple[str, ...]:
        """The privileges ``install.sh`` puts in the ``NetBoxSync`` role (read-only)."""
        return ("VM.Audit", "Sys.Audit", self.guest_agent_privileges[0])

    def check_supported(self) -> list[str]:
        """Return warnings; raise if the version is definitely unsupported."""
        if self.major < MIN_SUPPORTED_MAJOR:
            raise FatalStartupError(
                f"Proxmox VE {self.version.version or 'unknown'} is not supported (need PVE 8 or 9)")
        if self.major > MAX_TESTED_MAJOR:
            return [f"Proxmox VE {self.version.version} is newer than the tested versions (8, 9); continuing"]
        return []


# Task types (the ``type`` field of a UPID) that can change something we sync.
# Each one only *triggers* a re-read of the VM; the data itself always comes
# from the current VM config, so a wrong or duplicated trigger is harmless.
RELEVANT_TASK_TYPES: dict[str, str] = {
    "qmcreate": "created",
    "qmrestore": "restored",
    "qmclone": "cloned",      # id is the SOURCE vmid; the new VM is caught by the resource watcher
    "qmdestroy": "destroyed",
    "qmconfig": "config changed",
    "resize": "disk resized",
    "qmresize": "disk resized",
    "qmrollback": "snapshot rollback",
    "qmtemplate": "converted to template",
    "qmstart": "started",
    "qmreboot": "rebooted",
    "qmreset": "reset",
    "qmresume": "resumed",
    "qmstop": "stopped",
    "qmshutdown": "shut down",
    "hastart": "started (HA)",
    "hastop": "stopped (HA)",
    "qmigrate": "migrated",   # node is not synced, but the VM may get a new IP on some setups
    "move_volume": "disk moved",
    "qmmove": "disk moved",
}
# Tasks after which the guest agent is (or will soon be) able to report an IP.
START_TASK_TYPES = frozenset({"qmstart", "qmreboot", "qmreset", "qmresume", "hastart", "qmigrate"})
