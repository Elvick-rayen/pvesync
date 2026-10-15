"""Integration tests against a REAL NetBox (Proxmox is still faked).

Never point these at production: they create and delete objects. Run e.g.::

    NETBOX_IT_URL=http://127.0.0.1:18033 NETBOX_IT_TOKEN=... pytest -m integration

They verify the NetBox behaviour the design depends on: filters, validation
order for primary IPs, cascade deletion, version-specific fields.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from netbox_pve_sync.config import parse_config
from netbox_pve_sync.errors import ConflictError
from netbox_pve_sync.netbox.client import EP_CLUSTERS, EP_IPS, EP_VM_INTERFACES, EP_VMS, NetBoxClient
from netbox_pve_sync.netbox.setup import ensure_prerequisites
from netbox_pve_sync.proxmox.client import ProxmoxClient
from netbox_pve_sync.service import SyncService
from netbox_pve_sync.state import StateStore

from .fakes import FakeProxmox, agent_iface

URL = os.environ.get("NETBOX_IT_URL")
TOKEN = os.environ.get("NETBOX_IT_TOKEN")
# Optional: an admin token for test setup, so NETBOX_IT_TOKEN can be a least-privilege agent token.
ADMIN_TOKEN = os.environ.get("NETBOX_IT_ADMIN_TOKEN") or TOKEN
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not (URL and TOKEN), reason="NETBOX_IT_URL / NETBOX_IT_TOKEN not set")]


def _config(tmp_path: Path, cluster: str, **sync: Any):  # type: ignore[no-untyped-def]
    return parse_config({
        "netbox": {"url": URL, "token": TOKEN, "cluster": cluster},
        "proxmox": {"token_id": "t@pve!t", "token_secret": "s" * 36},
        "sync": sync,
        "retry": {"jitter": 0},
        "state": {"path": str(tmp_path / "state.db")},
    }, env={})


@pytest.fixture
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    assert URL and TOKEN
    cluster = f"IT-{uuid.uuid4().hex[:8]}"
    cfg = _config(tmp_path, cluster)
    nb = NetBoxClient(dataclasses.replace(cfg.netbox, token=ADMIN_TOKEN))
    ensure_prerequisites(nb, cfg.netbox.tag)
    ctype = nb.find_one("/api/virtualization/cluster-types/", "cluster type", slug="it-type") or nb.create(
        "/api/virtualization/cluster-types/", {"name": "it-type", "slug": "it-type"}, describe="type")
    cl = nb.create(EP_CLUSTERS, {"name": cluster, "type": ctype["id"]}, describe="cluster")
    px = FakeProxmox()
    yield cfg, nb, px, cl
    for vm in nb.list_all(EP_VMS, cluster_id=cl["id"]):
        nb.delete(EP_VMS, vm["id"], describe="cleanup")
    for ip in nb.list_all(EP_IPS, q="198.51.100."):
        nb.delete(EP_IPS, ip["id"], describe="cleanup")
    nb.delete(EP_CLUSTERS, cl["id"], describe="cleanup")


def service_for(cfg, px: FakeProxmox, **kw: Any) -> SyncService:  # type: ignore[no-untyped-def]
    svc = SyncService(cfg, proxmox=ProxmoxClient(cfg.proxmox, session=px), netbox=NetBoxClient(cfg.netbox),
                      state=StateStore(cfg.state_path), **kw)
    svc.check_netbox()
    return svc


def test_version_logged_and_supported(env) -> None:  # type: ignore[no-untyped-def]
    _, nb, _, _ = env
    assert nb.compat.check_supported() == [] or nb.compat.version.major > 4
    print(f"NetBox under test: {nb.compat.version}, disk unit {nb.compat.disk_unit_name}")


def test_full_lifecycle(env) -> None:  # type: ignore[no-untyped-def]
    cfg, nb, px, _cl = env
    svc = service_for(cfg, px)
    vm = px.add_vm(101, "it-web01", memory=8192, cores=2, sockets=2,
                   disks={"scsi0": "x:vm-101-disk-0,size=100G"}, ip="198.51.100.50/24")
    px.add_vm(102, "it-keep", ip=None, mac="BC:24:11:00:00:02")

    # create
    r = svc.sync_vmid(101, force=True)
    assert r.outcome == "created", r
    found = svc.synchronizer.find_owned(101)
    assert len(found) == 1
    nbvm = found[0]
    assert nbvm["memory"] == 8192 and float(nbvm["vcpus"]) == 4
    assert nbvm["disk"] == nb.compat.disk_value(100 * 1024**3)
    assert nbvm["primary_ip4"]["address"] == "198.51.100.50/24"
    ifaces = nb.vm_interfaces(nbvm["id"])
    assert [i["name"] for i in ifaces] == ["eth0"]

    # idempotent: three more forced syncs change nothing
    for _ in range(3):
        assert svc.sync_vmid(101, force=True).changes == []
    assert len(nb.vm_interfaces(nbvm["id"])) == 1
    assert len(nb.ips_by_address("198.51.100.50", None)) == 1

    # update everything incl. rename and IP change
    vm.config.update(name="it-web01-renamed", memory=4096, cores=4,
                     scsi1="x:vm-101-disk-1,size=50G")
    vm.agent_ifaces = [agent_iface("ens18", "bc:24:11:00:00:01", "198.51.100.60/24")]
    r = svc.sync_vmid(101, force=True)
    assert r.outcome == "updated", r
    nbvm = nb.get(EP_VMS, nbvm["id"])
    assert nbvm["name"] == "it-web01-renamed" and nbvm["memory"] == 4096 and float(nbvm["vcpus"]) == 8
    assert nbvm["disk"] == nb.compat.disk_value(150 * 1024**3)
    assert nbvm["primary_ip4"]["address"] == "198.51.100.60/24"
    assert nb.ips_by_address("198.51.100.50", None) == [], "old managed IP deleted"

    # delete: managed IP removed with the VM
    del px.vms[101]
    assert svc.sync_vmid(101).outcome == "deleted"
    assert svc.synchronizer.find_owned(101) == []
    assert nb.ips_by_address("198.51.100.60", None) == []


def test_foreign_ip_kept_and_not_stolen(env) -> None:  # type: ignore[no-untyped-def]
    cfg, nb, px, cl = env
    svc = service_for(cfg, px)
    # An IP documented by hand (unassigned) is claimed but survives VM deletion.
    doc = nb.create(EP_IPS, {"address": "198.51.100.70/24", "description": "by hand"}, describe="doc ip")
    px.add_vm(101, "it-claim", ip="198.51.100.70/24")
    px.add_vm(102, "it-keep", ip=None, mac="BC:24:11:00:00:02")
    svc.sync_vmid(101, force=True)
    assert svc.synchronizer.find_owned(101)[0]["primary_ip4"]["id"] == doc["id"]
    del px.vms[101]
    svc.sync_vmid(101)
    kept = nb.get(EP_IPS, doc["id"])
    assert kept["assigned_object_type"] is None

    # An IP assigned to another VM is not taken.
    other = nb.create(EP_VMS, {"name": "it-manual", "cluster": cl["id"]}, describe="manual vm")
    oif = nb.create(EP_VM_INTERFACES, {"virtual_machine": other["id"], "name": "eth0"}, describe="if")
    nb.create(EP_IPS, {"address": "198.51.100.80/24", "assigned_object_type": "virtualization.vminterface",
                       "assigned_object_id": oif["id"]}, describe="taken ip")
    px.add_vm(103, "it-thief", ip="198.51.100.80/24", mac="BC:24:11:00:00:03")
    with pytest.raises(ConflictError):
        svc.sync_vmid(103, force=True)
    ip = nb.ips_by_address("198.51.100.80", None)[0]
    assert ip["assigned_object_id"] == oif["id"]
    # the manual VM itself is untouched by reconciliation
    svc.reconcile()
    assert nb.get(EP_VMS, other["id"])["name"] == "it-manual"


def test_netbox_cascade_behaviour_documented(env) -> None:  # type: ignore[no-untyped-def]
    """Deleting a VM in NetBox deletes its interfaces AND the IPs assigned to them."""
    _, nb, _, cl = env
    vm = nb.create(EP_VMS, {"name": "it-cascade", "cluster": cl["id"]}, describe="vm")
    iface = nb.create(EP_VM_INTERFACES, {"virtual_machine": vm["id"], "name": "eth0"}, describe="if")
    ip = nb.create(EP_IPS, {"address": "198.51.100.90/24", "assigned_object_type": "virtualization.vminterface",
                            "assigned_object_id": iface["id"]}, describe="ip")
    nb.update(EP_VMS, vm["id"], {"primary_ip4": ip["id"]}, describe="primary")
    # Newer NetBox refuses to unassign an IP that is still primary; 3.3 allows it (leaving a dangling
    # primary). The agent always clears primary_ip4 first, which is correct on both.
    from netbox_pve_sync.errors import ValidationError
    try:
        nb.update(EP_IPS, ip["id"], {"assigned_object_type": None, "assigned_object_id": None}, describe="x")
        print(f"NetBox {nb.compat.version}: unassigning a primary IP is ALLOWED")
        nb.update(EP_IPS, ip["id"], {"assigned_object_type": "virtualization.vminterface",
                                     "assigned_object_id": iface["id"]}, describe="re-assign")
    except ValidationError:
        print(f"NetBox {nb.compat.version}: unassigning a primary IP is REJECTED")
    nb.delete(EP_VMS, vm["id"], describe="vm")
    assert nb.ips_by_address("198.51.100.90", None) == [], "NetBox cascades VM -> interface -> IP"


def test_name_filters_and_unique_names(env) -> None:  # type: ignore[no-untyped-def]
    cfg, nb, px, cl = env
    nb.create(EP_VMS, {"name": "It-Case", "cluster": cl["id"]}, describe="vm")
    assert len(nb.vms_in_cluster(cl["id"], name__ie="it-case")) == 1
    svc = service_for(cfg, px)
    px.add_vm(101, "it-case")
    with pytest.raises(ConflictError):
        svc.sync_vmid(101, force=True)


def test_dry_run_against_real_netbox(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    cfg, nb, px, cl = env
    px.add_vm(101, "it-dry")
    svc = SyncService(cfg, proxmox=ProxmoxClient(cfg.proxmox, session=px), netbox=NetBoxClient(cfg.netbox),
                      state=StateStore(cfg.state_path), dry_run=True)
    svc.check_netbox()
    report = svc.reconcile()
    assert report.created == [101]
    assert nb.vms_in_cluster(cl["id"]) == []
