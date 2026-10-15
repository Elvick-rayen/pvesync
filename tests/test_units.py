"""Pure unit tests: config, VM config parsing, IP selection, version compatibility, logging, backoff."""

from __future__ import annotations

import itertools
import logging
from pathlib import Path

import pytest

from netbox_pve_sync.backoff import backoff_delay
from netbox_pve_sync.config import IpSelectionConfig, load_config, parse_config, parse_duration
from netbox_pve_sync.errors import ConfigError, FatalStartupError
from netbox_pve_sync.ipselect import select_management_ipv4
from netbox_pve_sync.logging_setup import RedactingFilter, TextFormatter, mask_secret
from netbox_pve_sync.netbox.client import NetBoxCompat, NetBoxVersion, auth_header
from netbox_pve_sync.proxmox.compat import PveCompat, PveVersion
from netbox_pve_sync.proxmox.vmconfig import parse_memory, parse_size, parse_vm_config

from .conftest import raw_config
from .fakes import agent_iface

ENV = {"NETBOX_API_TOKEN": "nbtoken-abcdef123456", "PVE_TOKEN_SECRET": "pvesecret-123456"}


# ================================================================== config
def test_minimal_config_defaults() -> None:
    cfg = parse_config(raw_config(), env=ENV)
    assert cfg.netbox.token == "nbtoken-abcdef123456"
    assert cfg.sync.source_id == "PVE-Test"
    assert cfg.sync.interface_name == "eth0"
    assert cfg.sync.reconciliation_interval == 6 * 3600
    assert cfg.retry.max_delay == 3600
    assert cfg.proxmox.verify_ssl is True and cfg.netbox.verify_ssl is True
    assert "nbtoken" not in repr(cfg), "secrets must not appear in repr()"


def test_missing_env_var_is_clear_error() -> None:
    with pytest.raises(ConfigError, match="NETBOX_API_TOKEN is not set"):
        parse_config(raw_config(), env={"PVE_TOKEN_SECRET": "x"})


def test_unknown_option_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown option"):
        parse_config(raw_config(sync={"reconcile_intervall": "6h"}), env=ENV)


@pytest.mark.parametrize(("bad", "match"), [
    ({"netbox": {"url": "netbox:8000"}}, "http"),
    ({"proxmox": {"token_id": "root"}}, "token_id"),
    ({"sync": {"ip_cleanup_policy": "nuke"}}, "ip_cleanup_policy"),
    ({"retry": {"initial_delay": "2h", "max_delay": "1h"}}, "max_delay"),
    ({"ip_selection": {"prefer_networks": ["not-a-net"]}}, "invalid network"),
    ({"netbox": {"cluster": ""}}, "required"),
])
def test_invalid_values(bad: dict, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_config(raw_config(**bad), env=ENV)


@pytest.mark.parametrize(("text", "seconds"), [("5s", 5), ("10m", 600), ("6h", 21600), ("1d", 86400), (30, 30),
                                               ("1.5h", 5400), ("250ms", 0.25)])
def test_durations(text: object, seconds: float) -> None:
    assert parse_duration(text, name="x") == seconds


def test_load_config_with_secrets_file(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        'netbox:\n  url: "http://192.168.1.218:8000"\n  token: "${NETBOX_API_TOKEN}"\n  cluster: "PVE-Cyprus"\n'
        'proxmox:\n  token_id: "netbox-sync@pve!agent"\n  token_secret: "${PVE_TOKEN_SECRET}"\n'
        'sync:\n  reconciliation_interval: 12h\n')
    (tmp_path / "secrets.env").write_text("# comment\nNETBOX_API_TOKEN='from-file-123456'\n"
                                          "export PVE_TOKEN_SECRET=abc-123456\n")
    cfg = load_config(tmp_path / "config.yaml", env={})
    assert cfg.netbox.token == "from-file-123456"
    assert cfg.proxmox.token_secret == "abc-123456"
    assert cfg.netbox.cluster == "PVE-Cyprus"
    assert cfg.sync.reconciliation_interval == 43200
    # The real environment wins over the file.
    cfg = load_config(tmp_path / "config.yaml", env={"NETBOX_API_TOKEN": "from-env-123456"})
    assert cfg.netbox.token == "from-env-123456"


def test_example_config_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.example.yaml", env=ENV)
    assert cfg.netbox.url == "http://192.168.1.218:8000"
    assert cfg.sync.interface_name == "eth0"


# ================================================================== VM config
REAL_CONFIG = {  # from a real PVE node (VM 2014), trimmed
    "agent": "1,fstrim_cloned_disks=1", "balloon": "0", "cores": 6, "cpu": "host",
    "ide2": "none,media=cdrom", "memory": "16384", "name": "inmind-GPU",
    "net0": "virtio=96:B8:42:A8:A9:9E,bridge=vmbr1,firewall=1,tag=201",
    "scsi0": "NewDirectoryPVE2:2014/vm-2014-disk-0.qcow2,size=100G,ssd=1", "sockets": 2,
}


def test_real_config() -> None:
    p = parse_vm_config(REAL_CONFIG)
    assert p.name == "inmind-GPU"
    assert p.memory_mb == 16384
    assert p.vcpus == 12
    assert p.disk_bytes() == 100 * 1024**3
    assert p.agent_enabled is True
    assert p.macs == ["96:b8:42:a8:a9:9e"]


def test_multiple_disks_summed_and_non_disks_ignored() -> None:
    p = parse_vm_config({
        "scsi0": "local-lvm:vm-1-disk-0,size=32G", "scsi1": "zfs:vm-1-disk-1,size=600G",
        "virtio2": "ceph:vm-1-disk-2,size=512M", "sata0": "local:iso/x.iso,media=cdrom",
        "ide2": "none,media=cdrom", "unused0": "local:vm-1-disk-9,size=1000G",
        "efidisk0": "local-lvm:vm-1-disk-3,efitype=4m,size=4M", "tpmstate0": "local-lvm:vm-1-disk-4,size=4M",
    })
    assert p.disk_bytes() == 32 * 1024**3 + 600 * 1024**3 + 512 * 1024**2
    assert [d.key for d in p.disks] == ["scsi0", "scsi1", "virtio2"]
    p2 = parse_vm_config({"scsi0": "x:y,size=10G", "efidisk0": "x:z,size=4M"}, include_efi_tpm_disks=True)
    assert p2.disk_bytes() == 10 * 1024**3 + 4 * 1024**2


def test_disk_without_size_warns() -> None:
    p = parse_vm_config({"scsi0": "/dev/disk/by-id/ata-XYZ"})
    assert p.disk_bytes() == 0
    assert p.warnings


@pytest.mark.parametrize(("value", "mb"), [(4096, 4096), ("8192", 8192), ("current=2048", 2048), (None, 512)])
def test_memory_formats(value: object, mb: int) -> None:
    assert parse_memory(value) == mb


def test_vcpus_rules() -> None:
    assert parse_vm_config({"cores": 4, "sockets": 4}).vcpus == 16
    assert parse_vm_config({"cores": 4, "sockets": 2, "vcpus": 3}).vcpus == 3
    assert parse_vm_config({}).vcpus == 1


def test_agent_flag_variants() -> None:
    assert parse_vm_config({"agent": "1"}).agent_enabled
    assert parse_vm_config({"agent": "enabled=1,type=virtio"}).agent_enabled
    assert not parse_vm_config({"agent": "0"}).agent_enabled
    assert not parse_vm_config({}).agent_enabled


def test_sizes() -> None:
    assert parse_size("1T") == 1024**4
    assert parse_size("1536") == 1536
    assert parse_size("junk") is None


# ================================================================== IP selection
CFG = IpSelectionConfig()
MAC = "bc:24:11:00:00:01"


def pick(ifaces: list[dict], macs: list[str] | None = None, cfg: IpSelectionConfig = CFG) -> str | None:
    sel = select_management_ipv4(ifaces, [MAC] if macs is None else macs, cfg)
    return sel.selected.cidr if sel.selected else None


def test_single_ip() -> None:
    assert pick([agent_iface("eth0", MAC, "192.168.1.50/24")]) == "192.168.1.50/24"


def test_loopback_ipv6_linklocal_rejected() -> None:
    ifaces = [agent_iface("lo", "00:00:00:00:00:00", "127.0.0.1/8", v6=("::1/128",)),
              agent_iface("ens18", MAC, "169.254.10.1/16", v6=("fe80::1/64", "2001:db8::5/64"))]
    sel = select_management_ipv4(ifaces, [MAC], CFG)
    assert sel.selected is None
    reasons = " ".join(r for _, _, r in sel.rejected)
    assert "not IPv4" in reasons and "excluded network" in reasons and "exclude_interfaces" in reasons


def test_docker_and_k8s_interfaces_rejected() -> None:
    ifaces = [agent_iface("docker0", "02:42:ac:11:00:01", "172.17.0.1/16"),
              agent_iface("br-1a2b3c", "02:42:00:00:00:02", "172.18.0.1/16"),
              agent_iface("veth12345", "aa:aa:aa:aa:aa:aa", "10.244.0.1/24"),
              agent_iface("cni0", "bb:bb:bb:bb:bb:bb", "10.42.0.1/24"),
              agent_iface("ens18", MAC, "192.168.1.50/24")]
    assert pick(ifaces) == "192.168.1.50/24"


def test_multiple_interfaces_prefers_proxmox_mac() -> None:
    ifaces = [agent_iface("wgx", "11:11:11:11:11:11", "10.8.0.2/24"),     # unknown MAC, reported first
              agent_iface("ens19", "bc:24:11:00:00:02", "10.0.0.5/24"),
              agent_iface("ens18", MAC, "192.168.1.50/24")]
    assert pick(ifaces, macs=[MAC, "bc:24:11:00:00:02"]) == "192.168.1.50/24"   # net0 first


def test_multiple_ips_on_one_interface_takes_first() -> None:
    assert pick([agent_iface("eth0", MAC, "192.168.1.50/24", "192.168.1.99/24")]) == "192.168.1.50/24"


def test_prefer_networks_overrides_order() -> None:
    cfg = IpSelectionConfig(prefer_networks=("192.168.1.0/24",))
    ifaces = [agent_iface("ens18", MAC, "10.0.0.5/24", "192.168.1.60/24")]
    assert pick(ifaces, cfg=cfg) == "192.168.1.60/24"


def test_prefer_interfaces() -> None:
    cfg = IpSelectionConfig(prefer_interfaces=("ens19",), prefer_pve_mac=False)
    ifaces = [agent_iface("ens18", MAC, "10.0.0.5/24"), agent_iface("ens19", "x", "192.168.1.60/24")]
    assert pick(ifaces, cfg=cfg) == "192.168.1.60/24"


def test_require_pve_mac() -> None:
    cfg = IpSelectionConfig(require_pve_mac=True)
    assert pick([agent_iface("eth5", "de:ad:be:ef:00:01", "192.168.1.70/24")], cfg=cfg) is None


def test_private_internal_address_still_allowed_when_only_one() -> None:
    assert pick([agent_iface("ens18", MAC, "10.10.10.10/24")]) == "10.10.10.10/24"


def test_windows_style_names_and_zone_ids() -> None:
    ifaces = [{"name": "Loopback Pseudo-Interface 1", "ip-addresses": [
                  {"ip-address-type": "ipv4", "ip-address": "127.0.0.1", "prefix": 8}]},
              {"name": "Ethernet", "hardware-address": MAC, "ip-addresses": [
                  {"ip-address-type": "ipv6", "ip-address": "fe80::1%4", "prefix": 64},
                  {"ip-address-type": "ipv4", "ip-address": "192.168.1.80", "prefix": 24}]}]
    assert pick(ifaces) == "192.168.1.80/24"


def test_malformed_data_does_not_crash() -> None:
    ifaces = [{"name": "eth0", "ip-addresses": "garbage"},
              {"name": "eth1", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "999.1.1.1"},
                                                "junk", {"ip-address-type": "ipv4", "ip-address": "10.0.0.1",
                                                         "prefix": 99}]},
              {"name": "eth2", "hardware-address": MAC,
               "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "192.168.1.5", "prefix": 24}]}]
    sel = select_management_ipv4(ifaces, [MAC], CFG)
    assert sel.malformed
    assert sel.selected is not None and sel.selected.cidr == "192.168.1.5/24"


def test_network_and_broadcast_addresses_rejected() -> None:
    assert pick([agent_iface("eth0", MAC, "192.168.1.255/24", "192.168.1.0/24")]) is None
    assert pick([agent_iface("eth0", MAC, "10.0.0.1/32")]) == "10.0.0.1/32"


# ================================================================== compatibility
def test_pve8_vs_pve9_guest_agent_privilege() -> None:
    pve8 = PveCompat(PveVersion.from_api({"version": "8.2.4", "release": "8.2"}))
    pve9 = PveCompat(PveVersion.from_api({"version": "9.1.1", "release": "9.1"}))
    assert pve8.guest_agent_privileges == ("VM.Monitor",)
    assert "VM.GuestAgent.Audit" in pve9.guest_agent_privileges
    assert "VM.Monitor" not in pve9.role_privileges
    assert pve8.check_supported() == [] and pve9.check_supported() == []


def test_pve_unsupported_versions() -> None:
    with pytest.raises(FatalStartupError):
        PveCompat(PveVersion.from_api({"version": "7.4-3"})).check_supported()
    assert PveCompat(PveVersion.from_api({"version": "10.0.1"})).check_supported()  # warning only


def test_netbox_version_differences() -> None:
    nb3 = NetBoxCompat(NetBoxVersion.parse("3.3.10"))
    nb4 = NetBoxCompat(NetBoxVersion.parse("v4.2.1-Docker-3.2.0"))
    assert nb3.disk_value(100 * 1024**3) == 100
    assert nb4.disk_value(100 * 1024**3) == 102400
    assert nb3.custom_field_types_key == "content_types"
    assert nb4.custom_field_types_key == "object_types"
    with pytest.raises(FatalStartupError):
        NetBoxCompat(NetBoxVersion.parse("3.2.9")).check_supported()
    assert NetBoxCompat(NetBoxVersion.parse("5.0.0")).check_supported()


def test_auth_header_v1_and_v2_tokens() -> None:
    assert auth_header("0123456789abcdef") == "Token 0123456789abcdef"
    assert auth_header("nbt_ABC.secret") == "Bearer nbt_ABC.secret"


# ================================================================== logging / backoff
def test_secrets_are_redacted() -> None:
    f = RedactingFilter(["super-secret-token-value"])
    rec = logging.LogRecord("x", logging.ERROR, "f", 1, "failed with super-secret-token-value and "
                            "Authorization: Token abcdef and PVEAPIToken=u@pve!t=uuid-1234", None, None)
    rec.ctx = {"detail": "token super-secret-token-value"}
    f.filter(rec)
    text = TextFormatter(with_time=False).format(rec)
    assert "super-secret" not in text
    assert "abcdef" not in text
    assert "uuid-1234" not in text
    assert mask_secret("abcdefghijklmnop") == "abcd…(16 chars)"


def test_backoff_grows_and_caps() -> None:
    delays = [backoff_delay(n, initial=5, maximum=3600, jitter=0) for n in range(1, 15)]
    assert delays[:4] == [5, 10, 20, 40]
    assert max(delays) == 3600
    assert all(b >= a for a, b in itertools.pairwise(delays))
