"""Event detection, persistent retry queue, NetBox downtime, restarts, reconciliation and the daemon loop."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from netbox_pve_sync.daemon import EXIT_CONFIG, Daemon
from netbox_pve_sync.leader import LeaderElector
from netbox_pve_sync.netbox.client import NetBoxClient
from netbox_pve_sync.proxmox.client import ProxmoxClient
from netbox_pve_sync.proxmox.events import EventDetector, parse_upid
from netbox_pve_sync.service import SyncService
from netbox_pve_sync.state import StateStore

from .conftest import FakeClock, ServiceFactory, make_config
from .fakes import FakeNetBox, FakeProxmox, agent_iface


def detector(service: SyncService) -> EventDetector:
    return EventDetector(service.proxmox, service.state)


# ================================================================== events
def test_first_poll_is_quiet_then_tasks_become_events(service: SyncService, proxmox: FakeProxmox) -> None:
    proxmox.add_vm(101, "web01")
    proxmox.add_task("qmstart", 101)              # history from before the agent
    det = detector(service)
    events, _ = det.poll()
    assert events == []
    proxmox.add_task("qmconfig", 101)
    proxmox.add_task("qmcreate", 102, finished=False)  # still running: not yet
    proxmox.add_task("vzdump", 101)                    # irrelevant type
    events, _ = det.poll()
    assert [(e.vmid, e.kind) for e in events] == [(101, "task")]
    assert "qmconfig" in events[0].detail


def test_running_task_reported_when_finished(service: SyncService, proxmox: FakeProxmox) -> None:
    det = detector(service)
    det.poll()
    proxmox.add_task("qmdestroy", 105, finished=False)
    assert det.poll()[0] == []
    proxmox.tasks[0].update(endtime=1, status="OK")
    events, _ = det.poll()
    assert [e.vmid for e in events] == [105]
    assert det.poll()[0] == [], "the same UPID is never reported twice"


def test_resource_diff_detects_cli_changes_clone_and_delete(service: SyncService, proxmox: FakeProxmox) -> None:
    proxmox.add_vm(101, "web01", memory=2048)
    proxmox.add_vm(102, "db01")
    det = detector(service)
    det.poll()
    proxmox.vms[101].config["memory"] = 4096        # `qm set 101 --memory 4096`: no task
    proxmox.add_vm(103, "clone-of-101")             # clone target
    del proxmox.vms[102]
    events = {e.vmid: e for e in det.poll()[0]}
    assert events[101].kind == "changed" and "maxmem" in events[101].detail
    assert events[103].kind == "appeared"
    assert events[102].kind == "disappeared"


def test_node_migration_alone_is_not_an_event(service: SyncService, proxmox: FakeProxmox) -> None:
    proxmox.nodes = [(1, "pve1"), (2, "pve2")]
    proxmox.add_vm(101, "web01")
    det = detector(service)
    det.poll()
    proxmox.vms[101].node = "pve2"
    assert det.poll()[0] == []


def test_start_event_flag(service: SyncService, proxmox: FakeProxmox) -> None:
    proxmox.add_vm(101, "web01", status="stopped")
    det = detector(service)
    det.poll()
    proxmox.vms[101].status = "running"
    proxmox.add_task("qmstart", 101)
    events, _ = det.poll()
    assert len(events) == 1 and events[0].started


def test_empty_resource_list_ignored(service: SyncService, proxmox: FakeProxmox) -> None:
    proxmox.add_vm(101, "web01")
    det = detector(service)
    det.poll()
    proxmox.vms.clear()
    assert det.poll()[0] == []
    assert service.state.snapshot(), "snapshot kept so the real state is compared next time"


def test_changes_while_agent_was_down_detected_after_restart(make_service: ServiceFactory,
                                                            proxmox: FakeProxmox) -> None:
    svc = make_service()
    proxmox.add_vm(101, "web01")
    EventDetector(svc.proxmox, svc.state).poll()
    svc.state.close()
    proxmox.vms[101].config["name"] = "renamed-while-down"
    svc2 = make_service()                            # same state.db
    events, _ = EventDetector(svc2.proxmox, svc2.state).poll()
    assert [(e.vmid, e.kind) for e in events] == [(101, "changed")]


def test_parse_upid() -> None:
    assert parse_upid("UPID:pve1:0000ABCD:00001234:65000000:qmcreate:101:root@pam:")["type"] == "qmcreate"
    assert parse_upid("garbage") == {}


# ================================================================== queue
def test_duplicate_events_collapse_into_one_sync(service: SyncService, proxmox: FakeProxmox,
                                                 netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    for _ in range(5):
        service.state.enqueue(101, "task")
    service.state.enqueue(101, "changed")
    assert len(service.state.queue_items()) == 1
    assert service.process_queue() == 1
    assert len(netbox.vms()) == 1


def test_netbox_down_queues_and_recovers(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox,
                                         clock: FakeClock) -> None:
    proxmox.add_vm(101, "web01")
    proxmox.add_vm(102, "web02", ip="192.168.1.51/24", mac="BC:24:11:00:00:02")
    service.state.enqueue(101, "task")
    service.state.enqueue(102, "task")
    netbox.available = False
    assert service.process_queue() == 0
    items = service.state.queue_items()
    assert {i.vmid for i in items} == {101, 102}, "nothing is lost"
    first_delay = service.netbox_down_until - clock.now
    assert 0 < first_delay <= 5.0001

    # Still down: backoff grows, and nothing is attempted before it expires.
    netbox.calls.clear()
    assert service.process_queue() == 0
    assert netbox.calls == []
    clock.advance(first_delay + 0.01)
    service.process_queue()
    second_delay = service.netbox_down_until - clock.now
    assert second_delay > first_delay

    netbox.available = True
    clock.advance(second_delay + 0.01)
    assert service.process_queue() == 2
    assert service.state.queue_items() == []
    assert len(netbox.vms()) == 2
    assert service.netbox_failures == 0


def test_queue_survives_restart(make_service: ServiceFactory, proxmox: FakeProxmox, netbox: FakeNetBox,
                                clock: FakeClock) -> None:
    svc = make_service()
    svc.check_netbox()
    proxmox.add_vm(101, "web01")
    netbox.available = False
    svc.state.enqueue(101, "qmcreate")
    svc.process_queue()
    svc.state.close()

    netbox.available = True
    clock.advance(3600)
    svc2 = make_service()  # "after reboot": new process, same SQLite file
    svc2.check_netbox()
    assert [i.vmid for i in svc2.state.queue_items()] == [101]
    assert svc2.process_queue() == 1
    assert netbox.vm_by_name("web01") is not None


def test_one_broken_vm_does_not_block_others(service: SyncService, proxmox: FakeProxmox, netbox: FakeNetBox,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    proxmox.add_vm(101, "broken")
    proxmox.add_vm(102, "fine", ip="192.168.1.51/24", mac="BC:24:11:00:00:02")
    original = service.synchronizer.sync

    def flaky(desired, existing=None):  # type: ignore[no-untyped-def]
        if desired.vmid == 101:
            raise RuntimeError("boom")
        return original(desired, existing)
    monkeypatch.setattr(service.synchronizer, "sync", flaky)
    service.state.enqueue(101, "task")
    service.state.enqueue(102, "task")
    assert service.process_queue() == 1
    assert netbox.vm_by_name("fine") is not None
    item = service.state.queue_items()[0]
    assert item.vmid == 101 and item.attempts == 1 and "boom" in (item.last_error or "")


def test_retry_backoff_is_exponential_and_capped(service: SyncService, proxmox: FakeProxmox,
                                                 clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    proxmox.add_vm(101, "web01")
    monkeypatch.setattr(service.synchronizer, "sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    service.state.enqueue(101, "task")
    delays = []
    for _ in range(12):
        service.process_queue()
        item = service.state.queue_items()[0]
        delays.append(item.next_attempt - clock.now)
        clock.advance(item.next_attempt - clock.now + 0.01)
    assert delays[0] >= 60, "errors needing a human start slow"
    assert delays == sorted(delays)
    assert max(delays) <= 3600


def test_routine_check_does_not_shorten_backoff(service: SyncService, clock: FakeClock) -> None:
    service.state.enqueue(101, "task")
    service.state.defer(101, 600, "err")
    service.state.enqueue(101, "periodic check", urgent=False)
    assert service.state.queue_items()[0].next_attempt == pytest.approx(clock.now + 600)
    service.state.enqueue(101, "task", urgent=True)
    assert service.state.queue_items()[0].next_attempt == pytest.approx(clock.now)


def test_guest_agent_wait_backoff_and_timeout(make_service: ServiceFactory, proxmox: FakeProxmox,
                                              clock: FakeClock) -> None:
    svc = make_service(guest_agent={"ip_wait_timeout": "1h"})
    svc.check_netbox()
    vm = proxmox.add_vm(101, "web01")
    vm.agent_error = "QEMU guest agent is not running"
    svc.sync_vmid(101)
    first = svc.state.ip_wait_get(101)
    assert first is not None and first.next_check - clock.now == pytest.approx(30)
    clock.advance(31)
    svc.sync_vmid(101)
    second = svc.state.ip_wait_get(101)
    assert second is not None and second.next_check - clock.now == pytest.approx(60)
    clock.advance(3700)
    svc.sync_vmid(101)
    final = svc.state.ip_wait_get(101)
    assert final is not None and final.next_check >= 1e17, "after the timeout only periodic checks remain"
    vm.agent_ifaces = [agent_iface("ens18", "bc:24:11:00:00:01", "192.168.1.50/24")]
    vm.agent_error = None
    svc.sync_vmid(101)
    assert svc.state.ip_wait_get(101) is None


# ================================================================== reconciliation
def test_reconcile_creates_updates_and_deletes(service: SyncService, proxmox: FakeProxmox,
                                               netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    proxmox.add_vm(102, "web02", ip="192.168.1.51/24", mac="BC:24:11:00:00:02")
    proxmox.add_vm(103, "gone", ip="192.168.1.52/24", mac="BC:24:11:00:00:03")
    report = service.reconcile()
    assert sorted(report.created) == [101, 102, 103]

    # Missed events: VM 103 deleted, 102 changed, NetBox edited by hand, 104 created.
    del proxmox.vms[103]
    proxmox.vms[102].config["memory"] = 8192
    netbox.vm_by_name("web01")["memory"] = 1
    proxmox.add_vm(104, "new", ip=None)
    report = service.reconcile()
    assert report.created == [104]
    assert sorted(report.updated) == [101, 102]
    assert report.deleted == [103]
    assert netbox.vm_by_name("gone") is None
    assert netbox.vm_by_name("web01")["memory"] == 2048
    assert service.reconcile().summary()["unchanged"] == 3


def test_reconcile_mass_delete_guard(make_service: ServiceFactory, proxmox: FakeProxmox,
                                     netbox: FakeNetBox) -> None:
    svc = make_service(sync={"max_deletions_per_run": 2})
    svc.check_netbox()
    for i in range(5):
        proxmox.add_vm(100 + i, f"vm{i}", ip=f"192.168.1.{10 + i}/24", mac=f"BC:24:11:00:00:1{i}")
    proxmox.add_vm(200, "survivor", ip=None)
    svc.reconcile()
    for i in range(5):
        del proxmox.vms[100 + i]
    report = svc.reconcile()
    assert report.deleted == [] and len(report.skipped_deletions) == 5
    assert len(netbox.vms()) == 6
    report = svc.reconcile(allow_mass_delete=True)
    assert len(report.deleted) == 5


def test_reconcile_empty_proxmox_never_deletes(service: SyncService, proxmox: FakeProxmox,
                                               netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    service.reconcile()
    proxmox.vms.clear()
    report = service.reconcile(allow_mass_delete=True)
    assert report.skipped_deletions == [101]
    assert len(netbox.vms()) == 1


def test_reconcile_continues_after_conflict(service: SyncService, proxmox: FakeProxmox,
                                            netbox: FakeNetBox) -> None:
    netbox.add("ip-addresses", address="192.168.1.50/24", vrf=None, tags=[],
               assigned_object_type="dcim.interface", assigned_object_id=7)
    proxmox.add_vm(101, "web01")
    proxmox.add_vm(102, "web02", ip="192.168.1.51/24", mac="BC:24:11:00:00:02")
    report = service.reconcile()
    assert 101 in report.conflicts
    assert netbox.vm_by_name("web02")["primary_ip4"] is not None
    assert [i.vmid for i in service.state.queue_items()] == [101], "conflict is retried later"


def test_two_agents_share_one_netbox(tmp_path: Path, netbox: FakeNetBox, clock: FakeClock) -> None:
    netbox.add_cluster("PVE-Production")
    px_a, px_b = FakeProxmox(), FakeProxmox()
    px_a.add_vm(101, "cyprus-vm", ip="192.168.1.50/24")
    px_b.add_vm(101, "prod-vm", ip="10.0.0.50/24")  # same VMID in another environment
    services = []
    for name, px, cluster in (("a", px_a, "PVE-Test"), ("b", px_b, "PVE-Production")):
        (tmp_path / name).mkdir()
        cfg = make_config(tmp_path / name, netbox={"cluster": cluster})
        svc = SyncService(cfg, proxmox=ProxmoxClient(cfg.proxmox, session=px),
                          netbox=NetBoxClient(cfg.netbox, session=netbox),
                          state=StateStore(cfg.state_path, clock=clock), clock=clock)
        svc.check_netbox()
        svc.reconcile()
        services.append(svc)
    assert {v["name"] for v in netbox.vms()} == {"cyprus-vm", "prod-vm"}
    del px_a.vms[101]
    px_a.add_vm(102, "keep", ip=None)
    services[0].reconcile()
    assert {v["name"] for v in netbox.vms()} == {"keep", "prod-vm"}, "agent A never deletes agent B's VM"


# ================================================================== leader election / daemon
def test_leader_election_lowest_online_node(proxmox: FakeProxmox, service: SyncService) -> None:
    proxmox.nodes = [(1, "pve1"), (2, "pve2")]
    proxmox.cluster_name = "c1"
    proxmox.local_node = "pve2"
    elector = LeaderElector(service.proxmox)
    assert elector.check() is False and elector.active is False
    proxmox.offline_nodes = {"pve1"}
    assert elector.check() is True and elector.active is True
    proxmox.offline_nodes = set()
    elector.check()
    assert elector.active is False


def test_leader_election_with_agent_nodes(proxmox: FakeProxmox, service: SyncService) -> None:
    proxmox.nodes = [(1, "pve1"), (2, "pve2"), (3, "pve3")]
    proxmox.local_node = "pve3"
    elector = LeaderElector(service.proxmox, agent_nodes=("pve3", "pve2"))
    elector.check()
    assert elector.active


def test_standalone_node_is_active(service: SyncService) -> None:
    elector = LeaderElector(service.proxmox)
    elector.check()
    assert elector.active


def test_daemon_end_to_end_steps(make_service: ServiceFactory, proxmox: FakeProxmox, netbox: FakeNetBox,
                                 clock: FakeClock) -> None:
    svc = make_service()
    daemon = Daemon(svc.config, svc, clock=clock)
    assert daemon.startup()
    daemon.schedule_initial()
    proxmox.add_vm(101, "existing")
    daemon.step()                      # leader check, first event poll, startup reconciliation
    assert netbox.vm_by_name("existing") is not None

    proxmox.add_vm(102, "brand-new", ip="192.168.1.51/24", mac="BC:24:11:00:00:02")
    proxmox.add_task("qmcreate", 102)
    clock.advance(5)
    daemon.step()
    assert netbox.vm_by_name("brand-new") is not None

    # Guest IP changes have no Proxmox event: the periodic local check finds them.
    proxmox.vms[101].agent_ifaces = [agent_iface("ens18", "bc:24:11:00:00:01", "192.168.1.99/24")]
    clock.advance(601)
    daemon.step()
    vm = netbox.vm_by_name("existing")
    assert netbox.tables["ip-addresses"][vm["primary_ip4"]]["address"] == "192.168.1.99/24"

    del proxmox.vms[102]
    proxmox.add_task("qmdestroy", 102)
    clock.advance(5)
    daemon.step()
    assert netbox.vm_by_name("brand-new") is None


def test_daemon_survives_netbox_outage(make_service: ServiceFactory, proxmox: FakeProxmox, netbox: FakeNetBox,
                                       clock: FakeClock) -> None:
    svc = make_service()
    daemon = Daemon(svc.config, svc, clock=clock)
    daemon.startup()
    daemon.schedule_initial()
    daemon.step()
    netbox.available = False
    proxmox.add_vm(101, "during-outage")
    proxmox.add_task("qmcreate", 101)
    for _ in range(20):
        clock.advance(30)
        daemon.step()
    assert [i.vmid for i in svc.state.queue_items()] == [101]
    netbox.available = True
    for _ in range(10):
        clock.advance(600)
        daemon.step()
    assert netbox.vm_by_name("during-outage") is not None
    assert svc.state.queue_items() == []


def test_daemon_startup_fails_clearly_for_missing_cluster(make_service: ServiceFactory) -> None:
    svc = make_service(netbox={"cluster": "Does-Not-Exist"})
    daemon = Daemon(svc.config, svc)
    assert daemon.run() == EXIT_CONFIG


def test_daemon_startup_fails_for_bad_token(make_service: ServiceFactory, netbox: FakeNetBox) -> None:
    netbox.token = "something-else"
    svc = make_service()
    assert Daemon(svc.config, svc).run() == EXIT_CONFIG


def test_daemon_waits_for_netbox_at_startup(make_service: ServiceFactory, netbox: FakeNetBox) -> None:
    netbox.available = False
    svc = make_service(retry={"initial_delay": "10ms", "max_delay": "20ms"})
    daemon = Daemon(svc.config, svc)
    waits = []
    original_wait = daemon.stop_event.wait

    def fake_wait(timeout: float | None = None) -> bool:
        waits.append(timeout)
        if len(waits) == 3:
            netbox.available = True
        return original_wait(0)
    daemon.stop_event.wait = fake_wait  # type: ignore[method-assign]
    assert daemon.startup() is True
    assert len(waits) == 3


def test_sigterm_stops_loop(make_service: ServiceFactory) -> None:
    svc = make_service()
    daemon = Daemon(svc.config, svc)
    daemon.install_signal_handlers()
    try:
        signal.raise_signal(signal.SIGTERM)
        assert daemon.stopped()
        assert daemon.run() == 0
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)


def test_pve8_token_without_guest_agent_privilege_warns(make_service: ServiceFactory) -> None:
    px = FakeProxmox(version="8.2.4", privileges={"VM.Audit": 1, "Sys.Audit": 1})
    svc = make_service(px=px)
    warnings = svc.check_proxmox()
    assert any("VM.Monitor" in w for w in warnings)


def test_pve9_with_old_pve8_role_warns(make_service: ServiceFactory) -> None:
    px = FakeProxmox(version="9.0.3", privileges={"VM.Audit": 1, "Sys.Audit": 1, "VM.Monitor": 1})
    svc = make_service(px=px)
    warnings = svc.check_proxmox()
    assert any("VM.GuestAgent.Audit" in w for w in warnings)


@pytest.mark.parametrize("version", ["8.4.1", "9.1.1"])
def test_full_flow_on_pve8_and_pve9(make_service: ServiceFactory, netbox: FakeNetBox, version: str) -> None:
    px = FakeProxmox(version=version)
    px.add_vm(101, "web01", memory="current=4096" if version.startswith("9") else 4096)
    svc = make_service(px=px)
    assert svc.check_proxmox() == []
    svc.check_netbox()
    svc.reconcile()
    vm = netbox.vm_by_name("web01")
    assert vm["memory"] == 4096 and vm["primary_ip4"] is not None
