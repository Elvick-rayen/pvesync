"""In-memory fakes of the Proxmox VE and NetBox REST APIs.

They plug in at the HTTP session level, so the real client code (URL
building, pagination, error classification) is exercised too. The NetBox fake
enforces the validation rules that matter for ordering and safety:

* VM names are unique per cluster (case-insensitive);
* ``primary_ip4`` must be an IP assigned to an interface of that VM;
* an IP that is the primary IP of its VM cannot be unassigned or moved;
* interface names are unique per VM;
* a VM's site must match the site of its cluster, if the cluster has one;
* deleting a VM deletes its interfaces, and deleting an interface deletes the
  IPs assigned to it (NetBox cascade behaviour).
"""

from __future__ import annotations

import copy
import json as jsonlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests


class FakeResponse:
    def __init__(self, status_code: int, data: Any = None, reason: str = "") -> None:
        self.status_code = status_code
        self._data = data
        self.reason = reason or {200: "OK", 201: "Created", 204: "No Content"}.get(status_code, "Error")
        self.content = b"" if data is None else jsonlib.dumps(data).encode()
        self.text = self.content.decode()

    def json(self) -> Any:
        return self._data


class FakeSessionBase:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.available = True
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, params: dict[str, Any] | None = None, json: Any = None,
                data: Any = None, timeout: float | None = None) -> FakeResponse:
        if not self.available:
            raise requests.exceptions.ConnectionError("connection refused (fake)")
        path = urlparse(url).path
        self.calls.append((method, path))
        return self.handle(method, path, dict(params or {}), json)

    def handle(self, method: str, path: str, params: dict[str, Any], body: Any) -> FakeResponse:
        raise NotImplementedError


# ============================================================================ NetBox
class FakeNetBox(FakeSessionBase):
    def __init__(self, version: str = "3.3.10", token: str = "nb-test-token-1234567890") -> None:
        super().__init__()
        self.version = version
        self.token = token
        self.next_id = 100
        self.tables: dict[str, dict[int, dict[str, Any]]] = {
            "clusters": {}, "virtual-machines": {}, "interfaces": {}, "ip-addresses": {},
            "tags": {}, "custom-fields": {}, "vrfs": {}, "sites": {}, "cluster-groups": {},
        }
        self.writes: list[tuple[str, str, int | None]] = []

    # -------------------------------------------------------------- seeding helpers
    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def add(self, table: str, **obj: Any) -> dict[str, Any]:
        obj.setdefault("id", self._new_id())
        self.tables[table][obj["id"]] = obj
        return obj

    def add_cluster(self, name: str, site: int | None = None) -> dict[str, Any]:
        return self.add("clusters", name=name, site=site)

    def add_site(self, name: str, slug: str | None = None) -> dict[str, Any]:
        return self.add("sites", name=name, slug=slug or re.sub(r"[^a-z0-9]+", "-", name.lower()))

    def prepare(self, cluster: str = "PVE-Test", tag: str = "tag") -> None:
        self.add_cluster(cluster)
        self.add("tags", name=tag, slug=tag)
        key = "object_types" if int(self.version.split(".")[0]) >= 4 else "content_types"
        self.add("custom-fields", name="proxmox_vmid", type="integer", **{key: ["virtualization.virtualmachine"]})
        self.add("custom-fields", name="proxmox_source", type="text", **{key: ["virtualization.virtualmachine"]})

    def tag_id(self, slug: str = "tag") -> int:
        return next(t["id"] for t in self.tables["tags"].values() if t["slug"] == slug)

    def cluster_id(self, name: str = "PVE-Test") -> int:
        return next(c["id"] for c in self.tables["clusters"].values() if c["name"] == name)

    def vms(self) -> list[dict[str, Any]]:
        return list(self.tables["virtual-machines"].values())

    def vm_by_name(self, name: str) -> dict[str, Any] | None:
        return next((v for v in self.vms() if v["name"] == name), None)

    def interfaces_of(self, vm_id: int) -> list[dict[str, Any]]:
        return [i for i in self.tables["interfaces"].values() if i["virtual_machine"] == vm_id]

    def ips(self) -> list[dict[str, Any]]:
        return list(self.tables["ip-addresses"].values())

    def ips_of_interface(self, iface_id: int) -> list[dict[str, Any]]:
        return [ip for ip in self.ips() if ip.get("assigned_object_type") == "virtualization.vminterface"
                and ip.get("assigned_object_id") == iface_id]

    def write_count(self) -> int:
        return len(self.writes)

    # -------------------------------------------------------------- serialization
    def _ref(self, table: str, oid: int | None, *fields: str) -> dict[str, Any] | None:
        if oid is None:
            return None
        obj = self.tables[table].get(oid)
        if obj is None:
            return None
        return {"id": oid, **{f: obj.get(f) for f in fields}}

    def _serialize(self, table: str, obj: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(obj)
        if "tags" in out:
            out["tags"] = [{"id": t, "slug": self.tables["tags"][t]["slug"], "name": self.tables["tags"][t]["name"]}
                           for t in out["tags"] if t in self.tables["tags"]]
        if table == "virtual-machines":
            out["cluster"] = self._ref("clusters", obj.get("cluster"), "name")
            out["site"] = self._ref("sites", obj.get("site"), "name", "slug")
            if out.get("status") is not None:
                out["status"] = {"value": out["status"], "label": str(out["status"]).capitalize()}
            out["primary_ip4"] = self._ref("ip-addresses", obj.get("primary_ip4"), "address")
            out["primary_ip6"] = self._ref("ip-addresses", obj.get("primary_ip6"), "address")
            out.setdefault("custom_fields", {"proxmox_vmid": None, "proxmox_source": None})
        elif table == "clusters":
            out["site"] = self._ref("sites", obj.get("site"), "name", "slug")
            out["group"] = self._ref("cluster-groups", obj.get("group"), "name", "slug")
        elif table == "interfaces":
            out["virtual_machine"] = self._ref("virtual-machines", obj["virtual_machine"], "name")
        elif table == "ip-addresses":
            out["vrf"] = self._ref("vrfs", obj.get("vrf"), "name")
            if obj.get("assigned_object_type") == "virtualization.vminterface":
                iface = self.tables["interfaces"].get(obj.get("assigned_object_id") or -1)
                out["assigned_object"] = None if iface is None else {
                    "id": iface["id"], "name": iface["name"],
                    "virtual_machine": self._ref("virtual-machines", iface["virtual_machine"], "name")}
            elif obj.get("assigned_object_type"):
                out["assigned_object"] = {"id": obj.get("assigned_object_id"), "name": "Gi0/1",
                                          "device": {"id": 9999, "name": "core-switch"}}
            else:
                out["assigned_object"] = None
        return out

    # -------------------------------------------------------------- filters
    def _matches(self, table: str, obj: dict[str, Any], params: dict[str, Any]) -> bool:
        for key, value in params.items():
            if key in ("limit", "offset", "brief"):
                continue
            if key == "cluster_id" and obj.get("cluster") != int(value):
                return False
            if key.startswith("cf_"):
                cf = (obj.get("custom_fields") or {}).get(key[3:])
                if str(cf) != str(value):
                    return False
            if key == "tag":
                slugs = [self.tables["tags"][t]["slug"] for t in obj.get("tags") or []]
                if value not in slugs:
                    return False
            if key == "name" and obj.get("name") != value:
                return False
            if key == "name__ie" and str(obj.get("name", "")).lower() != str(value).lower():
                return False
            if key == "slug" and obj.get("slug") != value:
                return False
            if key == "virtual_machine_id" and obj.get("virtual_machine") != int(value):
                return False
            if key == "vminterface_id" and not (obj.get("assigned_object_type") == "virtualization.vminterface"
                                                and obj.get("assigned_object_id") == int(value)):
                return False
            if key == "address" and str(obj.get("address", "")).split("/")[0] != str(value).split("/")[0]:
                return False
            if key == "vrf_id":
                want = None if value == "null" else int(value)
                if obj.get("vrf") != want:
                    return False
        return True

    # -------------------------------------------------------------- validation
    def _error(self, message: str) -> FakeResponse:
        return FakeResponse(400, {"detail": message}, reason="Bad Request")

    def _validate_vm(self, obj: dict[str, Any]) -> str | None:
        for other in self.vms():
            if other["id"] != obj["id"] and other.get("cluster") == obj.get("cluster") \
                    and other["name"].lower() == obj["name"].lower():
                return "Virtual machine name must be unique per cluster."
        cluster = self.tables["clusters"].get(obj.get("cluster") or -1)
        if cluster and cluster.get("site") and obj.get("site") and cluster["site"] != obj["site"]:
            return "The selected cluster is not assigned to this site."
        primary = obj.get("primary_ip4")
        if primary is not None:
            ip = self.tables["ip-addresses"].get(primary)
            iface = self.tables["interfaces"].get(ip.get("assigned_object_id") or -1) if ip else None
            if ip is None or ip.get("assigned_object_type") != "virtualization.vminterface" or iface is None \
                    or iface["virtual_machine"] != obj["id"]:
                return "The specified IP address is not assigned to this VM."
        return None

    def _validate_ip_change(self, old: dict[str, Any], new: dict[str, Any]) -> str | None:
        moving = (old.get("assigned_object_type"), old.get("assigned_object_id")) != (
            new.get("assigned_object_type"), new.get("assigned_object_id"))
        if moving and old.get("assigned_object_type") == "virtualization.vminterface":
            iface = self.tables["interfaces"].get(old.get("assigned_object_id") or -1)
            if iface is not None:
                vm = self.tables["virtual-machines"].get(iface["virtual_machine"])
                if vm and old["id"] in (vm.get("primary_ip4"), vm.get("primary_ip6")):
                    return "Cannot reassign IP address while it is designated as the primary IP for the parent object"
        return None

    # -------------------------------------------------------------- routing
    def handle(self, method: str, path: str, params: dict[str, Any], body: Any) -> FakeResponse:
        auth = self.headers.get("Authorization", "")
        if path == "/api/status/":
            return FakeResponse(200, {"netbox-version": self.version, "django-version": "4.0",
                                      "rq-workers-running": 1})
        if auth not in (f"Token {self.token}", f"Bearer {self.token}"):
            return FakeResponse(403, {"detail": "Invalid token"}, reason="Forbidden")
        match = re.match(r"^/api/(\w+)/([\w-]+)/(?:(\d+)/)?$", path)
        if not match:
            return FakeResponse(404, {"detail": "Not found."}, reason="Not Found")
        table = match.group(2)
        oid = int(match.group(3)) if match.group(3) else None
        if table not in self.tables:
            return FakeResponse(404, {"detail": "Not found."}, reason="Not Found")
        rows = self.tables[table]

        if method == "GET":
            if oid is not None:
                if oid not in rows:
                    return FakeResponse(404, {"detail": "Not found."}, reason="Not Found")
                return FakeResponse(200, self._serialize(table, rows[oid]))
            found = [self._serialize(table, o) for o in sorted(rows.values(), key=lambda o: o["id"])
                     if self._matches(table, o, params)]
            offset, limit = int(params.get("offset", 0)), int(params.get("limit", 50))
            return FakeResponse(200, {"count": len(found), "next": None, "previous": None,
                                      "results": found[offset:offset + limit]})

        if method == "POST":
            obj = copy.deepcopy(body)
            obj["id"] = self._new_id()
            if table == "virtual-machines":
                obj.setdefault("primary_ip4", None)
                obj.setdefault("primary_ip6", None)
                obj.setdefault("tags", [])
                cf = {"proxmox_vmid": None, "proxmox_source": None}
                cf.update(obj.get("custom_fields") or {})
                obj["custom_fields"] = cf
                err = self._validate_vm(obj)
                if err:
                    return self._error(err)
            if table == "interfaces":
                if any(i["virtual_machine"] == obj["virtual_machine"] and i["name"] == obj["name"]
                       for i in rows.values()):
                    return self._error("Virtual machine and name must make a unique set.")
            if table == "ip-addresses":
                obj.setdefault("vrf", None)
                obj.setdefault("tags", [])
            rows[obj["id"]] = obj
            self.writes.append(("POST", table, obj["id"]))
            return FakeResponse(201, self._serialize(table, obj))

        if oid is None or oid not in rows:
            return FakeResponse(404, {"detail": "Not found."}, reason="Not Found")

        if method == "PATCH":
            old = rows[oid]
            new = copy.deepcopy(old)
            for key, value in body.items():
                if key == "custom_fields":
                    new.setdefault("custom_fields", {}).update(value)
                else:
                    new[key] = value
            if table == "virtual-machines":
                err = self._validate_vm(new)
                if err:
                    return self._error(err)
            if table == "ip-addresses":
                err = self._validate_ip_change(old, new)
                if err:
                    return self._error(err)
            rows[oid] = new
            self.writes.append(("PATCH", table, oid))
            return FakeResponse(200, self._serialize(table, new))

        if method == "DELETE":
            self._delete(table, oid)
            self.writes.append(("DELETE", table, oid))
            return FakeResponse(204)
        return FakeResponse(405, {"detail": "Method not allowed"})

    def _delete(self, table: str, oid: int) -> None:
        obj = self.tables[table].pop(oid)
        if table == "virtual-machines":
            for iface in list(self.interfaces_of(oid)):
                self._delete("interfaces", iface["id"])
        elif table == "interfaces":
            for ip in list(self.ips_of_interface(oid)):
                self._delete("ip-addresses", ip["id"])   # NetBox GenericRelation cascade
        elif table == "ip-addresses":
            for vm in self.vms():
                for key in ("primary_ip4", "primary_ip6"):
                    if vm.get(key) == obj["id"]:
                        vm[key] = None


# ============================================================================ Proxmox
@dataclass
class FakeVm:
    vmid: int
    node: str
    config: dict[str, Any]
    status: str = "running"
    agent_ifaces: list[dict[str, Any]] | None = None
    agent_error: str | None = None   # e.g. "QEMU guest agent is not running"
    template: bool = False
    tags: str = ""


def agent_iface(name: str, mac: str, *addrs: str, v6: tuple[str, ...] = ()) -> dict[str, Any]:
    ips = []
    for a in addrs:
        ip, prefix = a.split("/")
        ips.append({"ip-address-type": "ipv4", "ip-address": ip, "prefix": int(prefix)})
    for a in v6:
        ip, prefix = a.split("/")
        ips.append({"ip-address-type": "ipv6", "ip-address": ip, "prefix": int(prefix)})
    return {"name": name, "hardware-address": mac, "ip-addresses": ips}


def standard_ifaces(ip: str, mac: str = "bc:24:11:00:00:01") -> list[dict[str, Any]]:
    return [
        agent_iface("lo", "00:00:00:00:00:00", "127.0.0.1/8", v6=("::1/128",)),
        agent_iface("ens18", mac, ip, v6=("fe80::1/64",)),
    ]


@dataclass
class FakeProxmox(FakeSessionBase):
    version: str = "9.1.1"
    nodes: list[tuple[int, str]] = field(default_factory=lambda: [(1, "pve1")])
    local_node: str = "pve1"
    cluster_name: str | None = None
    offline_nodes: set[str] = field(default_factory=set)
    vms: dict[int, FakeVm] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    privileges: dict[str, int] | None = None
    task_counter: int = 0

    def __post_init__(self) -> None:
        FakeSessionBase.__init__(self)

    # -------------------------------------------------------------- helpers
    def add_vm(self, vmid: int, name: str, *, memory: Any = 2048, cores: int = 2, sockets: int = 1,
               disks: dict[str, str] | None = None, ip: str | None = "192.168.1.50/24",
               mac: str = "BC:24:11:00:00:01", agent: bool = True, status: str = "running",
               node: str = "pve1", **extra: Any) -> FakeVm:
        config: dict[str, Any] = {"name": name, "memory": memory, "cores": cores, "sockets": sockets,
                                  "net0": f"virtio={mac},bridge=vmbr0", "digest": "x"}
        config.update(disks if disks is not None else {"scsi0": f"local-lvm:vm-{vmid}-disk-0,size=32G"})
        if agent:
            config["agent"] = "1"
        config.update(extra)
        vm = FakeVm(vmid=vmid, node=node, config=config, status=status,
                    agent_ifaces=standard_ifaces(ip, mac.lower()) if ip else [])
        self.vms[vmid] = vm
        return vm

    def add_task(self, task_type: str, vmid: int, *, status: str = "OK", finished: bool = True,
                 node: str = "pve1") -> str:
        self.task_counter += 1
        upid = f"UPID:{node}:{self.task_counter:08X}:0000:6500{self.task_counter:04X}:{task_type}:{vmid}:root@pam:"
        task = {"upid": upid, "node": node, "type": task_type, "id": str(vmid), "user": "root@pam",
                "starttime": 1700000000 + self.task_counter}
        if finished:
            task["endtime"] = task["starttime"] + 1
            task["status"] = status
        self.tasks.insert(0, task)
        return upid

    # -------------------------------------------------------------- routing
    def _err(self, code: int, reason: str) -> FakeResponse:
        return FakeResponse(code, {"data": None}, reason=reason)

    def handle(self, method: str, path: str, params: dict[str, Any], body: Any) -> FakeResponse:
        assert method == "GET", "the agent must never write to Proxmox"
        if not self.headers.get("Authorization", "").startswith("PVEAPIToken="):
            return self._err(401, "authentication failure")
        p = path.removeprefix("/api2/json")
        if p == "/version":
            return FakeResponse(200, {"data": {"version": self.version, "release": self.version.rsplit(".", 1)[0],
                                               "repoid": "abc"}})
        if p == "/cluster/status":
            data: list[dict[str, Any]] = []
            if self.cluster_name:
                data.append({"type": "cluster", "name": self.cluster_name, "nodes": len(self.nodes), "quorate": 1})
            for nodeid, name in self.nodes:
                data.append({"type": "node", "name": name, "nodeid": nodeid,
                             "online": 0 if name in self.offline_nodes else 1,
                             "local": 1 if name == self.local_node else 0})
            return FakeResponse(200, {"data": data})
        if p == "/access/permissions":
            privs = self.privileges
            if privs is None:
                ga = "VM.GuestAgent.Audit" if int(self.version.split(".")[0]) >= 9 else "VM.Monitor"
                privs = {"VM.Audit": 1, "Sys.Audit": 1, ga: 1}
            return FakeResponse(200, {"data": {"/": privs, "/vms": privs}})
        if p == "/cluster/resources":
            data = []
            for vm in self.vms.values():
                cfg = vm.config
                mem = cfg.get("memory", 512)
                mem_mb = int(str(mem).replace("current=", ""))
                data.append({"type": "qemu", "vmid": vm.vmid, "id": f"qemu/{vm.vmid}", "node": vm.node,
                             "name": cfg.get("name"), "status": vm.status, "template": 1 if vm.template else 0,
                             "maxmem": mem_mb * 1024**2,
                             "maxcpu": int(cfg.get("cores", 1)) * int(cfg.get("sockets", 1)),
                             "maxdisk": 34359738368, "tags": vm.tags, "mem": 123, "cpu": 0.05})
            data.append({"type": "lxc", "vmid": 999, "node": "pve1", "name": "ct", "status": "running"})
            return FakeResponse(200, {"data": data})
        if p == "/cluster/tasks":
            return FakeResponse(200, {"data": copy.deepcopy(self.tasks)})
        m = re.match(r"^/nodes/([\w-]+)/qemu/(\d+)/(config|agent/network-get-interfaces)$", p)
        if m:
            node, vmid, what = m.group(1), int(m.group(2)), m.group(3)
            vm = self.vms.get(vmid)
            if vm is None or vm.node != node:
                return self._err(500, f"Configuration file 'nodes/{node}/qemu-server/{vmid}.conf' does not exist")
            if what == "config":
                return FakeResponse(200, {"data": copy.deepcopy(vm.config)})
            if "agent" not in vm.config or vm.config["agent"] in ("0", 0):
                return self._err(500, "No QEMU guest agent configured")
            if vm.status != "running":
                return self._err(500, f"VM {vmid} is not running")
            if vm.agent_error:
                return self._err(500, vm.agent_error)
            return FakeResponse(200, {"data": {"result": copy.deepcopy(vm.agent_ifaces)}})
        return self._err(501, f"Method 'GET {p}' not implemented")
