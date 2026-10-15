"""CLI commands, run through ``main()`` with fake HTTP sessions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from netbox_pve_sync import cli
from netbox_pve_sync.netbox import client as nb_client_mod
from netbox_pve_sync.proxmox import client as px_client_mod

from .conftest import NB_TOKEN, PVE_SECRET, raw_config
from .fakes import FakeNetBox, FakeProxmox


@pytest.fixture
def cfg_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, netbox: FakeNetBox,
             proxmox: FakeProxmox) -> Path:
    raw = raw_config()
    raw["state"] = {"path": str(tmp_path / "state.db")}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    (tmp_path / "secrets.env").write_text(f"NETBOX_API_TOKEN={NB_TOKEN}\nPVE_TOKEN_SECRET={PVE_SECRET}\n")
    monkeypatch.setattr(nb_client_mod, "make_session", lambda *a: netbox)
    monkeypatch.setattr(px_client_mod, "make_session", lambda *a: proxmox)
    monkeypatch.delenv("NETBOX_API_TOKEN", raising=False)
    return path


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    rc = cli.main(list(args))
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = run(capsys, "version")
    assert rc == 0 and "netbox-pve-sync 1." in out


def test_check_config(cfg_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = run(capsys, "-c", str(cfg_file), "check-config")
    assert rc == 0 and "Configuration OK" in out and NB_TOKEN not in out


def test_bad_config_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out = run(capsys, "-c", str(tmp_path / "missing.yaml"), "check-config")
    assert rc == 78 and "not found" in out


def test_global_dry_run_previews_reconcile(cfg_file: Path, capsys: pytest.CaptureFixture[str],
                                           proxmox: FakeProxmox, netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    rc, out = run(capsys, "-c", str(cfg_file), "--dry-run")
    assert rc == 0
    assert "DRY RUN" in out and "CREATE" in out and "web01" in out
    assert netbox.writes == []


def test_sync_vmid_and_reconcile(cfg_file: Path, capsys: pytest.CaptureFixture[str], proxmox: FakeProxmox,
                                 netbox: FakeNetBox) -> None:
    proxmox.add_vm(101, "web01")
    proxmox.add_vm(102, "web02", ip="192.168.1.51/24", mac="BC:24:11:00:00:02")
    rc, out = run(capsys, "-c", str(cfg_file), "sync", "--vmid", "101", "--dry-run")
    assert rc == 0 and netbox.writes == []
    rc, out = run(capsys, "-c", str(cfg_file), "sync", "--vmid", "101")
    assert rc == 0 and "VM 101: created" in out
    rc, out = run(capsys, "-c", str(cfg_file), "reconcile")
    assert rc == 0 and len(netbox.vms()) == 2
    rc, out = run(capsys, "-c", str(cfg_file), "status", "-v")
    assert rc == 0 and "synced VMs:       2" in out and "192.168.1.51/24" in out


def test_test_connection(cfg_file: Path, capsys: pytest.CaptureFixture[str], netbox: FakeNetBox) -> None:
    rc, out = run(capsys, "-c", str(cfg_file), "test-connection")
    assert rc == 0, out
    assert "All checks passed" in out and NB_TOKEN not in out and PVE_SECRET not in out
    netbox.token = "rotated"
    rc, out = run(capsys, "-c", str(cfg_file), "test-connection")
    assert rc == 1 and "FAIL" in out


def test_init_netbox_creates_fields(cfg_file: Path, capsys: pytest.CaptureFixture[str],
                                    netbox: FakeNetBox) -> None:
    netbox.tables["custom-fields"].clear()
    netbox.tables["tags"].clear()
    rc, out = run(capsys, "-c", str(cfg_file), "init-netbox", "--dry-run")
    assert rc == 0 and "would create" in out and netbox.writes == []
    rc, out = run(capsys, "-c", str(cfg_file), "init-netbox")
    assert rc == 0
    names = {cf["name"] for cf in netbox.tables["custom-fields"].values()}
    assert names == {"proxmox_vmid", "proxmox_source"}
    assert all(cf["content_types"] == ["virtualization.virtualmachine"]
               for cf in netbox.tables["custom-fields"].values())
    rc, out = run(capsys, "-c", str(cfg_file), "init-netbox")
    assert "nothing to do" in out


def test_missing_prerequisites_fail_clearly(cfg_file: Path, capsys: pytest.CaptureFixture[str],
                                            netbox: FakeNetBox) -> None:
    netbox.tables["custom-fields"].clear()
    rc, out = run(capsys, "-c", str(cfg_file), "reconcile")
    assert rc == 78 and "init-netbox" in out


def test_explain_ip(cfg_file: Path, capsys: pytest.CaptureFixture[str], proxmox: FakeProxmox) -> None:
    proxmox.add_vm(101, "web01")
    rc, out = run(capsys, "-c", str(cfg_file), "explain-ip", "--vmid", "101")
    assert rc == 0
    assert "Management IPv4: 192.168.1.50/24" in out and "127.0.0.1" in out


def test_queue_commands(cfg_file: Path, capsys: pytest.CaptureFixture[str], netbox: FakeNetBox,
                        proxmox: FakeProxmox) -> None:
    proxmox.add_vm(101, "web01")
    netbox.available = False
    run(capsys, "-c", str(cfg_file), "status")
    from netbox_pve_sync.state import StateStore
    st = StateStore(cfg_file.parent / "state.db")
    st.enqueue(101, "task")
    st.close()
    rc, out = run(capsys, "-c", str(cfg_file), "queue")
    assert "VM 101" in out
    rc, out = run(capsys, "-c", str(cfg_file), "queue", "--retry-now")
    assert "1 queued" in out
    rc, out = run(capsys, "-c", str(cfg_file), "queue", "--clear")
    assert rc == 0


def test_netbox_down_is_temporary_error(cfg_file: Path, capsys: pytest.CaptureFixture[str],
                                        netbox: FakeNetBox) -> None:
    netbox.available = False
    rc, out = run(capsys, "-c", str(cfg_file), "reconcile")
    assert rc == 1 and "temporary" in out
