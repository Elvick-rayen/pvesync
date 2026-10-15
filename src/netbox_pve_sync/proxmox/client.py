"""Read-only Proxmox VE API client (``/api2/json``), authenticated with an API token.

The agent never writes to Proxmox: every method here is a GET.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import ProxmoxConfig
from ..errors import ApiError, NotFoundError, ProxmoxUnavailable, TransientApiError
from ..http import JsonHttp, SessionLike, make_session
from .compat import PveCompat, PveVersion


class GuestAgentUnavailable(ApiError):
    """The guest agent is disabled, not installed, not running or did not answer."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason  # "not_configured" | "not_running" | "timeout" | "error"


@dataclass(frozen=True)
class VmResource:
    """One QEMU VM as listed by ``GET /cluster/resources?type=vm``."""

    vmid: int
    node: str
    name: str
    status: str
    template: bool
    maxmem: int
    maxcpu: float
    maxdisk: int
    tags: tuple[str, ...]

    def change_signature(self) -> dict[str, Any]:
        """Fields whose change means 'look at this VM again'. ``node`` and runtime usage are excluded."""
        return {"name": self.name, "status": self.status, "template": self.template,
                "maxmem": self.maxmem, "maxcpu": self.maxcpu, "maxdisk": self.maxdisk,
                "tags": list(self.tags)}


@dataclass(frozen=True)
class ClusterInfo:
    cluster_name: str | None   # None for a standalone node
    local_node: str | None
    online_nodes: tuple[tuple[int, str], ...]  # (nodeid, name), sorted by nodeid


def _split_tags(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    raw = str(value).replace(",", ";").replace(" ", ";")
    return tuple(t.strip().lower() for t in raw.split(";") if t.strip())


class ProxmoxClient:
    def __init__(self, config: ProxmoxConfig, session: SessionLike | None = None) -> None:
        self.config = config
        sess: SessionLike = session if session is not None else make_session(config.verify_ssl, config.ca_bundle)
        sess.headers["Authorization"] = f"PVEAPIToken={config.token_id}={config.token_secret}"
        sess.headers["Accept"] = "application/json"
        self.http = JsonHttp(sess, f"{config.api_url}/api2/json", config.timeout,
                             transient_cls=ProxmoxUnavailable, service="Proxmox")
        self._compat: PveCompat | None = None

    def _get(self, path: str, **params: Any) -> Any:
        result = self.http.request("GET", path, params=params or None)
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    # ----------------------------------------------------------------- basic info
    def version(self) -> PveVersion:
        data = self._get("/version")
        if not isinstance(data, dict):
            raise ApiError("Proxmox /version returned unexpected data")
        return PveVersion.from_api(data)

    @property
    def compat(self) -> PveCompat:
        if self._compat is None:
            self._compat = PveCompat(self.version())
        return self._compat

    def cluster_info(self) -> ClusterInfo:
        data = self._get("/cluster/status") or []
        cluster_name = None
        local = None
        online: list[tuple[int, str]] = []
        for item in data:
            if item.get("type") == "cluster":
                cluster_name = item.get("name")
            elif item.get("type") == "node":
                if item.get("local"):
                    local = item.get("name")
                if item.get("online", 1):
                    online.append((int(item.get("nodeid") or 0), str(item.get("name"))))
        return ClusterInfo(cluster_name, local, tuple(sorted(online)))

    def permissions(self) -> dict[str, dict[str, int]]:
        """Effective privileges of this token, keyed by ACL path."""
        data = self._get("/access/permissions")
        return data if isinstance(data, dict) else {}

    # ----------------------------------------------------------------- VMs
    def list_vms(self) -> list[VmResource]:
        """All QEMU VMs in the cluster (LXC containers are ignored)."""
        data = self._get("/cluster/resources", type="vm") or []
        vms = []
        for item in data:
            if item.get("type") != "qemu" or item.get("vmid") is None:
                continue
            vms.append(VmResource(
                vmid=int(item["vmid"]),
                node=str(item.get("node") or ""),
                name=str(item.get("name") or ""),
                status=str(item.get("status") or "unknown"),
                template=bool(item.get("template")),
                maxmem=int(item.get("maxmem") or 0),
                maxcpu=float(item.get("maxcpu") or 0),
                maxdisk=int(item.get("maxdisk") or 0),
                tags=_split_tags(item.get("tags")),
            ))
        return vms

    def vm_config(self, node: str, vmid: int) -> dict[str, Any]:
        """The VM's configuration (pending changes included, which is what is *configured*)."""
        try:
            data = self._get(f"/nodes/{node}/qemu/{vmid}/config")
        except TransientApiError as exc:
            if exc.status == 500 and exc.body and "does not exist" in exc.body:
                raise NotFoundError(f"VM {vmid} does not exist on node {node}", status=404) from exc
            raise
        if not isinstance(data, dict):
            raise ApiError(f"Unexpected config data for VM {vmid}")
        return data

    def guest_network_interfaces(self, node: str, vmid: int) -> list[dict[str, Any]]:
        """``agent/network-get-interfaces``: the guest's own view of its NICs and addresses."""
        try:
            data = self._get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        except TransientApiError as exc:
            body = (exc.body or "").lower()
            if exc.status is None:
                raise  # Proxmox itself is unreachable
            if "not configured" in body:
                raise GuestAgentUnavailable(f"QEMU guest agent is not enabled for VM {vmid}",
                                            reason="not_configured") from exc
            if "not running" in body:
                raise GuestAgentUnavailable(f"QEMU guest agent is not running in VM {vmid}",
                                            reason="not_running") from exc
            if "timeout" in body or "got timeout" in body:
                raise GuestAgentUnavailable(f"QEMU guest agent of VM {vmid} timed out",
                                            reason="timeout") from exc
            if "does not exist" in body:
                raise NotFoundError(f"VM {vmid} does not exist", status=404) from exc
            raise GuestAgentUnavailable(f"QEMU guest agent of VM {vmid} returned an error: {exc.body}",
                                        reason="error") from exc
        if isinstance(data, dict):
            data = data.get("result", [])
        if not isinstance(data, list):
            raise GuestAgentUnavailable(f"Malformed guest agent network data for VM {vmid}", reason="error")
        return [d for d in data if isinstance(d, dict)]

    # ----------------------------------------------------------------- tasks
    def cluster_tasks(self) -> list[dict[str, Any]]:
        """Recent tasks of all nodes (the same list the web UI's task panel shows)."""
        data = self._get("/cluster/tasks") or []
        return [t for t in data if isinstance(t, dict)]
