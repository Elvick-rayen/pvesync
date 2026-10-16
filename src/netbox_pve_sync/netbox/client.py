"""NetBox REST API client with version compatibility and dry-run support.

Version differences that matter to this agent (checked against the NetBox
release notes and tested against NetBox 3.3.10 and 4.x containers):

=====================  ======================  ==============================
Topic                  NetBox 3.x              NetBox 4.x
=====================  ======================  ==============================
``VirtualMachine.disk``  gigabytes              megabytes (changed in 4.0)
custom field types     ``content_types``       ``object_types`` (4.0)
token header           ``Token <key>``         ``Token`` (v1) or ``Bearer nbt_…`` (v2, 4.5+)
=====================  ======================  ==============================

Everything else used here (``/api/status/``, virtual machines, VM interfaces,
IP addresses with ``assigned_object_type``/``assigned_object_id``,
``primary_ip4``, ``cf_<name>`` filters, tags) is identical in 3.3 – 4.x.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import NetBoxConfig
from ..errors import FatalStartupError, NetBoxUnavailable
from ..http import JsonHttp, SessionLike, make_session
from ..logging_setup import get_logger, log

logger = get_logger("netbox")

MIN_SUPPORTED = (3, 3)
MAX_TESTED_MAJOR = 4
PAGE_SIZE = 200

EP_VMS = "/api/virtualization/virtual-machines/"
EP_CLUSTERS = "/api/virtualization/clusters/"
EP_VM_INTERFACES = "/api/virtualization/interfaces/"
EP_IPS = "/api/ipam/ip-addresses/"
EP_VRFS = "/api/ipam/vrfs/"
EP_SITES = "/api/dcim/sites/"
EP_TAGS = "/api/extras/tags/"
EP_CUSTOM_FIELDS = "/api/extras/custom-fields/"
VMINTERFACE_TYPE = "virtualization.vminterface"


@dataclass(frozen=True)
class NetBoxVersion:
    raw: str
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, raw: str) -> NetBoxVersion:
        match = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", raw.strip())
        if not match:
            raise FatalStartupError(f"Cannot parse NetBox version {raw!r}")
        return cls(raw=raw, major=int(match.group(1)), minor=int(match.group(2)),
                   patch=int(match.group(3) or 0))

    @property
    def tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class NetBoxCompat:
    version: NetBoxVersion

    @property
    def disk_unit_bytes(self) -> int:
        """Bytes per unit of ``VirtualMachine.disk``: GB before 4.0, MB since 4.0 (binary units, like Proxmox)."""
        return 1024**2 if self.version.major >= 4 else 1024**3

    @property
    def disk_unit_name(self) -> str:
        return "MB" if self.version.major >= 4 else "GB"

    def disk_value(self, disk_bytes: int) -> int:
        """Convert bytes to NetBox units, rounding to the nearest unit (a 0.4 GB disk counts as 0)."""
        return round(disk_bytes / self.disk_unit_bytes)

    @property
    def custom_field_types_key(self) -> str:
        return "object_types" if self.version.major >= 4 else "content_types"

    def check_supported(self) -> list[str]:
        if self.version.tuple[:2] < MIN_SUPPORTED:
            raise FatalStartupError(
                f"NetBox {self.version} is not supported; netbox-pve-sync needs NetBox "
                f"{MIN_SUPPORTED[0]}.{MIN_SUPPORTED[1]} or newer")
        if self.version.major > MAX_TESTED_MAJOR:
            return [f"NetBox {self.version} is newer than the tested versions (3.3 - 4.x); continuing"]
        return []


def auth_header(token: str) -> str:
    """NetBox 4.5+ v2 tokens (``nbt_<key>.<secret>``) use Bearer; legacy v1 tokens use ``Token``."""
    if token.startswith("nbt_"):
        return f"Bearer {token}"
    return f"Token {token}"


def obj_id(value: Any) -> int | None:
    """NetBox returns related objects as nested dicts; accept dicts, ints or None."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    return int(value)


@dataclass
class PlannedChange:
    action: str   # create | update | delete
    object_type: str
    description: str
    data: dict[str, Any] = field(default_factory=dict)


class NetBoxClient:
    def __init__(self, config: NetBoxConfig, session: SessionLike | None = None, *,
                 dry_run: bool = False) -> None:
        self.config = config
        sess: SessionLike = session if session is not None else make_session(config.verify_ssl, config.ca_bundle)
        sess.headers["Authorization"] = auth_header(config.token)
        sess.headers["Accept"] = "application/json"
        sess.headers["Content-Type"] = "application/json"
        self.http = JsonHttp(sess, config.url, config.timeout, transient_cls=NetBoxUnavailable,
                             service="NetBox")
        self.dry_run = dry_run
        self.planned: list[PlannedChange] = []
        self._fake_id = 0
        self._compat: NetBoxCompat | None = None

    # ------------------------------------------------------------- version
    def status(self) -> dict[str, Any]:
        data = self.http.request("GET", "/api/status/")
        if not isinstance(data, dict) or "netbox-version" not in data:
            raise FatalStartupError("NetBox /api/status/ did not return a version; is this really NetBox?")
        return data

    @property
    def compat(self) -> NetBoxCompat:
        if self._compat is None:
            self._compat = NetBoxCompat(NetBoxVersion.parse(str(self.status()["netbox-version"])))
        return self._compat

    def check_token(self) -> None:
        """``/api/status/`` may be readable anonymously, so make one authenticated call."""
        self.http.request("GET", EP_VMS, params={"limit": 1, "brief": 1})

    # ------------------------------------------------------------- generic helpers
    def list_all(self, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.http.request("GET", endpoint, params={**params, "limit": PAGE_SIZE, "offset": offset})
            if not isinstance(page, dict):
                break
            batch = page.get("results") or []
            results.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(page.get("count") or 0):
                break
        return results

    def get(self, endpoint: str, object_id: int) -> dict[str, Any]:
        return self.http.request("GET", f"{endpoint}{object_id}/")  # type: ignore[no-any-return]

    def create(self, endpoint: str, data: dict[str, Any], *, describe: str) -> dict[str, Any]:
        if self.dry_run:
            self._fake_id -= 1
            self.planned.append(PlannedChange("create", endpoint, describe, data))
            return {"id": self._fake_id, **data}
        log(logger, logging.DEBUG, "NetBox create", endpoint=endpoint, object=describe)
        return self.http.request("POST", endpoint, json=data)  # type: ignore[no-any-return]

    def update(self, endpoint: str, object_id: int, data: dict[str, Any], *, describe: str) -> dict[str, Any]:
        if self.dry_run:
            self.planned.append(PlannedChange("update", endpoint, describe, data))
            return {"id": object_id, **data}
        log(logger, logging.DEBUG, "NetBox update", endpoint=endpoint, id=object_id, object=describe,
            fields=sorted(data))
        return self.http.request("PATCH", f"{endpoint}{object_id}/", json=data)  # type: ignore[no-any-return]

    def delete(self, endpoint: str, object_id: int, *, describe: str) -> None:
        if self.dry_run:
            self.planned.append(PlannedChange("delete", endpoint, describe, {"id": object_id}))
            return
        log(logger, logging.DEBUG, "NetBox delete", endpoint=endpoint, id=object_id, object=describe)
        self.http.request("DELETE", f"{endpoint}{object_id}/")

    # ------------------------------------------------------------- lookups
    def find_one(self, endpoint: str, what: str, **params: Any) -> dict[str, Any] | None:
        found = self.list_all(endpoint, **params)
        if len(found) > 1:
            raise FatalStartupError(f"More than one NetBox {what} matches {params}; names must be unique")
        return found[0] if found else None

    def cluster(self, name: str) -> dict[str, Any] | None:
        return self.find_one(EP_CLUSTERS, "cluster", name=name)

    def tag(self, slug: str) -> dict[str, Any] | None:
        return self.find_one(EP_TAGS, "tag", slug=slug)

    def vrf(self, name: str) -> dict[str, Any] | None:
        return self.find_one(EP_VRFS, "VRF", name=name)

    def site(self, name: str) -> dict[str, Any] | None:
        """Look a site up by name (as shown in the UI dropdown), falling back to its slug."""
        return self.find_one(EP_SITES, "site", name=name) or self.find_one(EP_SITES, "site", slug=name)

    def custom_field(self, name: str) -> dict[str, Any] | None:
        return self.find_one(EP_CUSTOM_FIELDS, "custom field", name=name)

    def vms_in_cluster(self, cluster_id: int, **params: Any) -> list[dict[str, Any]]:
        return self.list_all(EP_VMS, cluster_id=cluster_id, **params)

    def vm_interfaces(self, vm_id: int) -> list[dict[str, Any]]:
        if vm_id < 0:  # object only planned in dry-run mode
            return []
        return self.list_all(EP_VM_INTERFACES, virtual_machine_id=vm_id)

    def ips_on_interface(self, interface_id: int) -> list[dict[str, Any]]:
        if interface_id < 0:
            return []
        return self.list_all(EP_IPS, vminterface_id=interface_id)

    def ips_by_address(self, address: str, vrf_id: int | None) -> list[dict[str, Any]]:
        """All IP objects with this host address (any prefix length) in the given VRF (None = global)."""
        host = address.split("/")[0]
        found = self.list_all(EP_IPS, address=host, vrf_id=vrf_id if vrf_id is not None else "null")
        # Filter again client-side: be strict about host and VRF whatever the server-side filter did.
        return [ip for ip in found
                if str(ip.get("address", "")).split("/")[0] == host and obj_id(ip.get("vrf")) == vrf_id]
