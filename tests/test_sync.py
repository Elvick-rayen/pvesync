"""VM create / update / delete, IP handling, idempotency and ownership against the fake NetBox."""

from __future__ import annotations

import pytest

from netbox_pve_sync.errors import ConflictError
from netbox_pve_sync.service import SyncService

from .conftest import ServiceFactory
from .fakes import FakeNetBox, FakeProxmox, agent_iface

VMI = "virtualization.vminterface"


def nb_vm(netbox: FakeNetBox, name: str) -> dict:
    vm = netbox.vm_by_name(name)
    assert vm is not None, f"VM {name} not in NetBox"
    return vm


def eth0(netbox: FakeNetBox, vm: dict) -> dict:
    ifaces = [i for i in netbox.interfaces_of(vm["id"]) if i["name"] == "eth0"]
    assert len(ifaces) == 1
    return ifaces[0]


# ---------------------------------------------------------------- creation
def test_create_vm_with_eth0_ip_and_primary(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01", memory=8192, cores=2, sockets=2,
                   disks={"scsi0": "local-lvm:vm-101-disk-0,size=100G"}, ip="192.168.1.50/24")
    result = service.sync_vmid(101)

    assert result.outcome == "created"
    vm = nb_vm(netbox, "web01")
    assert vm["cluster"] == netbox.cluster_id()
    assert vm["memory"] == 8192
    assert vm["vcpus"] == 4
    assert vm["disk"] == 100            # NetBox 3.x: GB
    assert vm["status"] == "active"
    assert vm["custom_fields"] == {"proxmox_vmid": 101, "proxmox_source": "PVE-Test"}
    assert netbox.tag_id() in vm["tags"]
    iface = eth0(netbox, vm)
    ips = netbox.ips_of_interface(iface["id"])
    assert [ip["address"] for ip in ips] == ["192.168.1.50/24"]
    assert vm["primary_ip4"] == ips[0]["id"]
    assert netbox.tag_id() in ips[0]["tags"]


def test_sync_four_times_is_idempotent(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101, force=True)
    writes = netbox.write_count()
    for _ in range(3):
        result = service.sync_vmid(101, force=True)
        assert result.outcome == "unchanged"
    assert netbox.write_count() == writes, "repeated syncs must not write"
    assert len(netbox.vms()) == 1
    assert len(netbox.interfaces_of(nb_vm(netbox, "web01")["id"])) == 1
    assert len(netbox.ips()) == 1


def test_unchanged_vm_needs_no_netbox_calls(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    netbox.calls.clear()
    assert service.sync_vmid(101).outcome == "unchanged"
    assert netbox.calls == [], "fingerprint should short-circuit the NetBox comparison"


def test_netbox_4_disk_in_megabytes(make_service: ServiceFactory, proxmox: FakeProxmox) -> None:
    nb4 = FakeNetBox(version="4.2.1", token="nb-test-token-1234567890")
    nb4.prepare()
    svc = make_service(nb=nb4)
    svc.check_netbox()
    proxmox.add_vm(101, "web01", disks={"scsi0": "x:vm-101-disk-0,size=100G", "scsi1": "x:vm-101-disk-1,size=50G"})
    svc.sync_vmid(101)
    assert nb4.vm_by_name("web01")["disk"] == 150 * 1024


def test_vm_created_before_guest_agent_gets_ip_later(service: SyncService, proxmox: FakeProxmox,
                                                     netbox: FakeNetBox) -> None:
    vm = proxmox.add_vm(101, "web01")
    vm.agent_error = "QEMU guest agent is not running"
    result = service.sync_vmid(101)
    assert result.outcome == "created"
    nbvm = nb_vm(netbox, "web01")
    assert nbvm["primary_ip4"] is None
    assert len(netbox.interfaces_of(nbvm["id"])) == 1  # eth0 exists already
    assert service.state.ip_wait_get(101) is not None, "should be scheduled for an IP re-check"

    vm.agent_error = None
    service.sync_vmid(101)
    assert nb_vm(netbox, "web01")["primary_ip4"] is not None
    assert service.state.ip_wait_get(101) is None


# ---------------------------------------------------------------- updates
@pytest.mark.parametrize(("change", "check"), [
    ({"memory": 8192}, lambda vm: vm["memory"] == 8192),
    ({"cores": 4}, lambda vm: vm["vcpus"] == 4),
    ({"scsi0": "local-lvm:vm-101-disk-0,size=100G"}, lambda vm: vm["disk"] == 100),
    ({"name": "new-name"}, lambda vm: vm["name"] == "new-name"),
])
def test_updates(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox, change: dict, check) -> None:
    proxmox.add_vm(101, "old-name", memory=4096, cores=2, disks={"scsi0": "local-lvm:vm-101-disk-0,size=50G"})
    service.sync_vmid(101)
    proxmox.vms[101].config.update(change)
    result = service.sync_vmid(101)
    assert result.outcome == "updated"
    assert len(netbox.vms()) == 1
    vm = netbox.vms()[0]
    assert check(vm)
    assert vm["custom_fields"]["proxmox_vmid"] == 101


def test_rename_keeps_same_netbox_object(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "old-name")
    service.sync_vmid(101)
    original_id = netbox.vms()[0]["id"]
    proxmox.vms[101].config["name"] = "new-name"
    service.sync_vmid(101)
    assert [v["id"] for v in netbox.vms()] == [original_id]
    assert netbox.vms()[0]["name"] == "new-name"


def test_ip_change_replaces_managed_ip(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    vm = proxmox.add_vm(101, "web01", ip="192.168.1.50/24")
    service.sync_vmid(101)
    vm.agent_ifaces = [agent_iface("ens18", "bc:24:11:00:00:01", "192.168.1.60/24")]
    result = service.sync_vmid(101)
    assert "primary_ip4 -> 192.168.1.60/24" in result.changes
    nbvm = nb_vm(netbox, "web01")
    ips = netbox.ips_of_interface(eth0(netbox, nbvm)["id"])
    assert [ip["address"] for ip in ips] == ["192.168.1.60/24"]
    assert nbvm["primary_ip4"] == ips[0]["id"]
    assert [ip["address"] for ip in netbox.ips()] == ["192.168.1.60/24"], "old managed IP is deleted"


def test_ip_change_with_unassign_policy_keeps_old_ip_object(make_service: ServiceFactory, proxmox: FakeProxmox,
                                                            netbox: FakeNetBox) -> None:
    svc = make_service(sync={"ip_cleanup_policy": "unassign"})
    svc.check_netbox()
    vm = proxmox.add_vm(101, "web01", ip="192.168.1.50/24")
    svc.sync_vmid(101)
    vm.agent_ifaces = [agent_iface("ens18", "bc:24:11:00:00:01", "192.168.1.60/24")]
    svc.sync_vmid(101)
    old = next(ip for ip in netbox.ips() if ip["address"] == "192.168.1.50/24")
    assert old["assigned_object_type"] is None


def test_prefix_change_updates_managed_ip(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    vm = proxmox.add_vm(101, "web01", ip="192.168.1.50/24")
    service.sync_vmid(101)
    vm.agent_ifaces = [agent_iface("ens18", "bc:24:11:00:00:01", "192.168.1.50/23")]
    service.sync_vmid(101)
    assert [ip["address"] for ip in netbox.ips()] == ["192.168.1.50/23"]


def test_agent_unavailable_keeps_existing_ip(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    vm = proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    vm.agent_error = "QEMU guest agent is not running"
    vm.config["memory"] = 4096
    result = service.sync_vmid(101)
    assert result.outcome == "updated"
    nbvm = nb_vm(netbox, "web01")
    assert nbvm["memory"] == 4096
    assert nbvm["primary_ip4"] is not None
    assert len(netbox.ips()) == 1


def test_stopped_vm_keeps_existing_ip(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    vm = proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    vm.status = "stopped"
    service.sync_vmid(101, force=True)
    assert nb_vm(netbox, "web01")["primary_ip4"] is not None
    assert service.state.ip_wait_get(101) is None, "stopped VMs are not polled"


def test_existing_eth0_is_reused(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    vm = nb_vm(netbox, "web01")
    iface_id = eth0(netbox, vm)["id"]
    netbox.tables["ip-addresses"].clear()
    netbox.tables["virtual-machines"][vm["id"]]["primary_ip4"] = None
    service.sync_vmid(101, force=True)
    assert eth0(netbox, vm)["id"] == iface_id
    assert len(netbox.interfaces_of(vm["id"])) == 1


def test_other_interfaces_and_manual_ips_left_alone(service: SyncService, proxmox: FakeProxmox,
                                                    netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    vm = nb_vm(netbox, "web01")
    extra_if = netbox.add("interfaces", virtual_machine=vm["id"], name="mgmt1")
    manual = netbox.add("ip-addresses", address="10.9.9.9/24", vrf=None, tags=[],
                        assigned_object_type=VMI, assigned_object_id=eth0(netbox, vm)["id"])
    service.sync_vmid(101, force=True)
    assert extra_if["id"] in netbox.tables["interfaces"]
    assert netbox.tables["ip-addresses"][manual["id"]]["assigned_object_id"] == eth0(netbox, vm)["id"]


# ---------------------------------------------------------------- IP conflicts
def test_ip_owned_by_other_object_is_not_stolen(service: SyncService, proxmox: FakeProxmox,
                                                netbox: FakeNetBox) -> None:
    taken = netbox.add("ip-addresses", address="192.168.1.50/24", vrf=None, tags=[],
                       assigned_object_type="dcim.interface", assigned_object_id=5)
    proxmox.add_vm(101, "web01", memory=4096)
    with pytest.raises(ConflictError):
        service.sync_vmid(101)
    vm = nb_vm(netbox, "web01")
    assert vm["memory"] == 4096, "the rest of the VM is still synchronized"
    assert vm["primary_ip4"] is None
    assert netbox.tables["ip-addresses"][taken["id"]]["assigned_object_type"] == "dcim.interface"
    assert service.state.fingerprint(101) is None, "conflicts are retried, not marked as done"


def test_ip_assigned_to_other_vm_is_not_stolen(service: SyncService, proxmox: FakeProxmox,
                                               netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01", ip="192.168.1.50/24")
    service.sync_vmid(101)
    proxmox.add_vm(102, "web02", ip="192.168.1.50/24", mac="BC:24:11:00:00:02")
    with pytest.raises(ConflictError):
        service.sync_vmid(102)
    ip = netbox.ips()[0]
    assert ip["assigned_object_id"] == eth0(netbox, nb_vm(netbox, "web01"))["id"]


def test_unassigned_existing_ip_is_claimed_without_tag(service: SyncService, proxmox: FakeProxmox,
                                                       netbox: FakeNetBox) -> None:
    doc = netbox.add("ip-addresses", address="192.168.1.50/24", vrf=None, tags=[], assigned_object_type=None,
                     assigned_object_id=None, description="documented by hand")
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    vm = nb_vm(netbox, "web01")
    assert len(netbox.ips()) == 1
    assert vm["primary_ip4"] == doc["id"]
    assert netbox.tag_id() not in netbox.tables["ip-addresses"][doc["id"]]["tags"]


def test_ip_in_other_vrf_is_ignored(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    vrf = netbox.add("vrfs", name="customer")
    netbox.add("ip-addresses", address="192.168.1.50/24", vrf=vrf["id"], tags=[],
               assigned_object_type="dcim.interface", assigned_object_id=5)
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)  # no conflict: different VRF
    assert len(netbox.ips()) == 2


# ---------------------------------------------------------------- deletion
def test_delete_removes_vm_interface_and_managed_ip(service: SyncService, proxmox: FakeProxmox,
                                                    netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    del proxmox.vms[101]
    proxmox.add_vm(102, "other")  # keep the list non-empty
    result = service.sync_vmid(101)
    assert result.outcome == "deleted"
    assert netbox.vm_by_name("web01") is None
    assert netbox.tables["interfaces"] == {} or all(
        i["virtual_machine"] != 101 for i in netbox.tables["interfaces"].values())
    assert all(ip["address"] != "192.168.1.50/24" for ip in netbox.ips())
    assert service.state.fingerprint(101) is None


def test_delete_keeps_ip_not_created_by_agent(service: SyncService, proxmox: FakeProxmox,
                                              netbox: FakeNetBox) -> None:
    doc = netbox.add("ip-addresses", address="192.168.1.50/24", vrf=None, tags=[], assigned_object_type=None,
                     assigned_object_id=None)
    proxmox.add_vm(101, "web01")
    proxmox.add_vm(102, "other", ip=None)
    service.sync_vmid(101)
    del proxmox.vms[101]
    service.sync_vmid(101)
    assert netbox.vm_by_name("web01") is None
    kept = netbox.tables["ip-addresses"].get(doc["id"])
    assert kept is not None, "an IP the agent did not create must survive the VM deletion"
    assert kept["assigned_object_type"] is None


def test_unassign_policy_keeps_managed_ip_on_delete(make_service: ServiceFactory, proxmox: FakeProxmox,
                                                    netbox: FakeNetBox) -> None:
    svc = make_service(sync={"ip_cleanup_policy": "unassign"})
    svc.check_netbox()
    proxmox.add_vm(101, "web01")
    proxmox.add_vm(102, "other", ip=None)
    svc.sync_vmid(101)
    del proxmox.vms[101]
    svc.sync_vmid(101)
    assert [ip["address"] for ip in netbox.ips()] == ["192.168.1.50/24"]


def test_empty_proxmox_list_never_deletes(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    proxmox.vms.clear()
    with pytest.raises(Exception, match="empty VM list"):
        service.sync_vmid(101)
    assert netbox.vm_by_name("web01") is not None


def test_template_is_not_synced_and_removed(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    proxmox.vms[101].template = True
    assert service.sync_vmid(101).outcome == "deleted"


# ---------------------------------------------------------------- ownership
def test_foreign_vms_are_never_touched(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    cid = netbox.cluster_id()
    other_cluster = netbox.add_cluster("PVE-Other")
    manual = netbox.add("virtual-machines", name="manual-vm", cluster=cid, tags=[], primary_ip4=None,
                        custom_fields={"proxmox_vmid": None, "proxmox_source": None})
    other_agent = netbox.add("virtual-machines", name="vm-from-other-pve", cluster=other_cluster["id"],
                             tags=[netbox.tag_id()], primary_ip4=None,
                             custom_fields={"proxmox_vmid": 101, "proxmox_source": "PVE-Other"})
    same_cluster_other_source = netbox.add(
        "virtual-machines", name="shared", cluster=cid, tags=[netbox.tag_id()], primary_ip4=None,
        custom_fields={"proxmox_vmid": 101, "proxmox_source": "Somebody-Else"})
    proxmox.add_vm(101, "web01")
    proxmox.add_vm(102, "x", ip=None)
    service.sync_vmid(101)
    del proxmox.vms[101]
    service.sync_vmid(101)
    service.reconcile()
    for obj in (manual, other_agent, same_cluster_other_source):
        assert obj["id"] in netbox.tables["virtual-machines"]
    assert ("PATCH", "virtual-machines", manual["id"]) not in netbox.writes
    assert ("PATCH", "virtual-machines", other_agent["id"]) not in netbox.writes


def test_name_conflict_with_unmanaged_vm(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    manual = netbox.add("virtual-machines", name="web01", cluster=netbox.cluster_id(), tags=[], primary_ip4=None,
                        custom_fields={"proxmox_vmid": None, "proxmox_source": None}, memory=1)
    proxmox.add_vm(101, "WEB01")
    with pytest.raises(ConflictError, match="adopt_existing"):
        service.sync_vmid(101)
    assert len(netbox.vms()) == 1
    assert netbox.tables["virtual-machines"][manual["id"]]["memory"] == 1


def test_adopt_existing(make_service: ServiceFactory, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    manual = netbox.add("virtual-machines", name="web01", cluster=netbox.cluster_id(), tags=[], primary_ip4=None,
                        custom_fields={"proxmox_vmid": None, "proxmox_source": None}, memory=1)
    svc = make_service(sync={"adopt_existing": True})
    svc.check_netbox()
    proxmox.add_vm(101, "web01", memory=2048)
    svc.sync_vmid(101)
    vm = netbox.tables["virtual-machines"][manual["id"]]
    assert len(netbox.vms()) == 1
    assert vm["memory"] == 2048
    assert vm["custom_fields"]["proxmox_vmid"] == 101
    assert netbox.tag_id() in vm["tags"]


def test_duplicate_owned_vms_are_collapsed(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    first = nb_vm(netbox, "web01")
    netbox.add("virtual-machines", name="web01-dup", cluster=netbox.cluster_id(), tags=[netbox.tag_id()],
               primary_ip4=None, custom_fields={"proxmox_vmid": 101, "proxmox_source": "PVE-Test"})
    service.sync_vmid(101, force=True)
    assert [v["id"] for v in netbox.vms()] == [first["id"]]


def test_rename_conflict_does_not_block_other_fields(service: SyncService, proxmox: FakeProxmox,
                                                     netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01", memory=1024)
    service.sync_vmid(101)
    netbox.add("virtual-machines", name="taken", cluster=netbox.cluster_id(), tags=[], primary_ip4=None,
               custom_fields={"proxmox_vmid": None, "proxmox_source": None})
    proxmox.vms[101].config.update(name="taken", memory=2048)
    with pytest.raises(ConflictError, match="cannot rename"):
        service.sync_vmid(101)
    vm = netbox.vms()[0]
    assert vm["name"] == "web01" and vm["memory"] == 2048


def test_existing_tags_preserved(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.sync_vmid(101)
    other = netbox.add("tags", name="prod", slug="prod")
    vm = nb_vm(netbox, "web01")
    vm["tags"].append(other["id"])
    proxmox.vms[101].config["memory"] = 9999
    service.sync_vmid(101)
    assert set(nb_vm(netbox, "web01")["tags"]) == {netbox.tag_id(), other["id"]}


# ---------------------------------------------------------------- dry run
def test_dry_run_writes_nothing(make_service: ServiceFactory, proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    svc = make_service(dry_run=True)
    svc.check_proxmox()
    svc.check_netbox()
    proxmox.add_vm(101, "web01")
    proxmox.add_vm(102, "web02", ip="192.168.1.51/24", mac="BC:24:11:00:00:02")
    report = svc.reconcile()
    assert netbox.writes == []
    assert sorted(report.created) == [101, 102]
    kinds = [(c.action, c.object_type) for c in svc.netbox.planned]
    assert kinds.count(("create", "/api/virtualization/virtual-machines/")) == 2
    assert kinds.count(("create", "/api/ipam/ip-addresses/")) == 2
    assert svc.state.fingerprint(101) is None
