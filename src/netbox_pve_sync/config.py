"""Configuration loading and validation.

Configuration lives in a YAML file (default ``/etc/netbox-pve-sync/config.yaml``).
Secrets are never written into that file directly: values such as
``token: "${NETBOX_API_TOKEN}"`` are expanded from the process environment and
from an optional ``secrets.env`` file that sits next to the config file
(mode 0640, owned by root and the service group).
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path("/etc/netbox-pve-sync/config.yaml")
DEFAULT_STATE_PATH = Path("/var/lib/netbox-pve-sync/state.db")

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$", re.IGNORECASE)
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Interfaces that never carry the VM's management address. Shell-style globs.
DEFAULT_EXCLUDE_INTERFACES = [
    "lo", "lo0", "Loopback*",
    "docker*", "br-*", "veth*", "virbr*", "lxcbr*", "lxdbr*", "podman*",
    "cni*", "flannel*", "cali*", "cilium*", "weave*", "kube-*", "vxlan*", "genev_sys_*",
    "tun*", "tap*", "wg*", "tailscale*", "zt*", "isatap*", "Teredo*",
    "vEthernet (WSL*", "vEthernet (Default Switch)",
]
# Networks that are never a management address.
DEFAULT_EXCLUDE_NETWORKS = [
    "0.0.0.0/8",        # "this network"
    "127.0.0.0/8",      # loopback
    "169.254.0.0/16",   # link-local / APIPA
    "172.17.0.0/16",    # Docker default bridge
    "224.0.0.0/4",      # multicast
    "240.0.0.0/4",      # reserved / broadcast
]


def parse_duration(value: Any, *, name: str) -> float:
    """Parse ``"5s"``, ``"10m"``, ``"6h"``, ``"1d"`` or a plain number of seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"{name}: expected a duration, got {value!r}")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        match = _DURATION_RE.match(value)
        if not match:
            raise ConfigError(f"{name}: invalid duration {value!r} (use e.g. 30s, 10m, 6h, 1d)")
        seconds = float(match.group(1)) * _DURATION_UNITS[(match.group(2) or "s").lower()]
    else:
        raise ConfigError(f"{name}: expected a duration, got {value!r}")
    if seconds <= 0:
        raise ConfigError(f"{name}: duration must be positive")
    return seconds


def format_duration(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"


def load_env_file(path: Path) -> dict[str, str]:
    """Read a simple ``KEY=value`` file (comments, blank lines and quotes allowed)."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    except PermissionError as exc:
        raise ConfigError(f"Cannot read secrets file {path}: permission denied") from exc
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ConfigError(f"{path}:{lineno}: expected KEY=value")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _expand(value: Any, env: dict[str, str], path: str) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            var, default = match.group(1), match.group(2)
            if var in env:
                return env[var]
            if default is not None:
                return default
            raise ConfigError(f"{path}: environment variable {var} is not set")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v, env, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


class _Section:
    """Small helper that reads typed values out of one YAML mapping and rejects unknown keys."""

    def __init__(self, data: Any, name: str) -> None:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"{name}: expected a mapping")
        self.data: dict[str, Any] = dict(data)
        self.name = name
        self.used: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.used.add(key)
        return self.data.get(key, default)

    def opt_str(self, key: str, default: str | None = None, *, required: bool = False) -> str | None:
        value = self.get(key, default)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            if required:
                raise ConfigError(f"{self.name}.{key} is required")
            return default
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ConfigError(f"{self.name}.{key}: expected a string")
        return str(value).strip()

    def opt_bool(self, key: str, default: bool) -> bool:
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "yes", "on", "1", "false", "no", "off", "0"):
            return value.lower() in ("true", "yes", "on", "1")
        raise ConfigError(f"{self.name}.{key}: expected true/false")

    def opt_int(self, key: str, default: int, *, minimum: int = 0) -> int:
        value = self.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{self.name}.{key}: expected an integer")
        if value < minimum:
            raise ConfigError(f"{self.name}.{key}: must be >= {minimum}")
        return value

    def opt_float(self, key: str, default: float, *, minimum: float = 0.0) -> float:
        value = self.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{self.name}.{key}: expected a number")
        if value < minimum:
            raise ConfigError(f"{self.name}.{key}: must be >= {minimum}")
        return float(value)

    def opt_duration(self, key: str, default: str) -> float:
        return parse_duration(self.get(key, default), name=f"{self.name}.{key}")

    def opt_str_list(self, key: str, default: list[str] | None = None) -> list[str]:
        value = self.get(key, list(default or []))
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, (str, int)) for v in value):
            raise ConfigError(f"{self.name}.{key}: expected a list of strings")
        return [str(v) for v in value]

    def opt_int_list(self, key: str) -> list[int]:
        value = self.get(key, [])
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            raise ConfigError(f"{self.name}.{key}: expected a list of integers")
        return list(value)

    def finish(self) -> None:
        unknown = sorted(set(self.data) - self.used)
        if unknown:
            raise ConfigError(f"{self.name}: unknown option(s): {', '.join(unknown)}")


@dataclass(frozen=True)
class NetBoxConfig:
    url: str
    token: str = field(repr=False)
    cluster: str
    verify_ssl: bool = True
    ca_bundle: str | None = None
    timeout: float = 15.0
    vrf: str | None = None
    tag: str = "tag"
    site: str | None = None


@dataclass(frozen=True)
class ProxmoxConfig:
    api_url: str
    token_id: str
    token_secret: str = field(repr=False)
    verify_ssl: bool = True
    ca_bundle: str | None = None
    timeout: float = 15.0
    leader_election: bool = True
    agent_nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyncConfig:
    source_id: str
    interface_name: str = "eth0"
    include_templates: bool = False
    exclude_vmids: tuple[int, ...] = ()
    exclude_pve_tags: tuple[str, ...] = ()
    event_poll_interval: float = 5.0
    local_check_interval: float = 600.0
    reconciliation_interval: float = 6 * 3600.0
    reconcile_on_startup: bool = True
    adopt_existing: bool = False
    claim_unassigned_ips: bool = True
    ip_cleanup_policy: str = "delete_managed"
    max_deletions_per_run: int = 10
    include_efi_tpm_disks: bool = False
    sync_status: bool = True


@dataclass(frozen=True)
class RetryConfig:
    initial_delay: float = 5.0
    max_delay: float = 3600.0
    multiplier: float = 2.0
    jitter: float = 0.1


@dataclass(frozen=True)
class GuestAgentConfig:
    ip_wait_initial_delay: float = 30.0
    ip_wait_max_delay: float = 900.0
    ip_wait_timeout: float = 86400.0


@dataclass(frozen=True)
class IpSelectionConfig:
    exclude_interfaces: tuple[str, ...] = tuple(DEFAULT_EXCLUDE_INTERFACES)
    exclude_networks: tuple[str, ...] = tuple(DEFAULT_EXCLUDE_NETWORKS)
    prefer_networks: tuple[str, ...] = ()
    prefer_interfaces: tuple[str, ...] = ()
    prefer_pve_mac: bool = True
    require_pve_mac: bool = False


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "text"


@dataclass(frozen=True)
class Config:
    netbox: NetBoxConfig
    proxmox: ProxmoxConfig
    sync: SyncConfig
    retry: RetryConfig
    guest_agent: GuestAgentConfig
    ip_selection: IpSelectionConfig
    logging: LoggingConfig
    state_path: Path
    source_path: Path | None = None

    def secrets(self) -> list[str]:
        """All secret values, so the log formatter can mask them if they ever leak into a message."""
        return [s for s in (self.netbox.token, self.proxmox.token_secret) if s]


def _validate_url(value: str, name: str) -> str:
    if not re.match(r"^https?://[^/\s]+", value):
        raise ConfigError(f"{name}: expected an http(s):// URL, got {value!r}")
    return value.rstrip("/")


def _validate_networks(values: list[str], name: str) -> tuple[str, ...]:
    for v in values:
        try:
            ipaddress.ip_network(v, strict=False)
        except ValueError as exc:
            raise ConfigError(f"{name}: invalid network {v!r}") from exc
    return tuple(values)


def parse_config(raw: Any, *, env: dict[str, str] | None = None, source_path: Path | None = None) -> Config:
    """Build a validated :class:`Config` from already-parsed YAML data."""
    env = dict(os.environ if env is None else env)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("Top level of the configuration must be a mapping")
    raw = _expand(raw, env, "config")
    top = _Section(raw, "config")

    nb = _Section(top.get("netbox"), "netbox")
    netbox = NetBoxConfig(
        url=_validate_url(nb.opt_str("url", required=True) or "", "netbox.url"),
        token=nb.opt_str("token", required=True) or "",
        cluster=nb.opt_str("cluster", required=True) or "",
        verify_ssl=nb.opt_bool("verify_ssl", True),
        ca_bundle=nb.opt_str("ca_bundle"),
        timeout=nb.opt_duration("timeout", "15s"),
        vrf=nb.opt_str("vrf"),
        tag=nb.opt_str("tag", "tag") or "tag",
        site=nb.opt_str("site"),
    )
    nb.finish()
    if not re.match(r"^[-a-z0-9_]+$", netbox.tag):
        raise ConfigError("netbox.tag must be a slug (lowercase letters, digits, - and _)")

    px = _Section(top.get("proxmox"), "proxmox")
    proxmox = ProxmoxConfig(
        api_url=_validate_url(px.opt_str("api_url", "https://127.0.0.1:8006") or "", "proxmox.api_url"),
        token_id=px.opt_str("token_id", required=True) or "",
        token_secret=px.opt_str("token_secret", required=True) or "",
        verify_ssl=px.opt_bool("verify_ssl", True),
        ca_bundle=px.opt_str("ca_bundle"),
        timeout=px.opt_duration("timeout", "15s"),
        leader_election=px.opt_bool("leader_election", True),
        agent_nodes=tuple(px.opt_str_list("agent_nodes")),
    )
    px.finish()
    if not re.match(r"^[^@!\s]+@[^@!\s]+![A-Za-z][A-Za-z0-9._-]*$", proxmox.token_id):
        raise ConfigError("proxmox.token_id must look like 'user@realm!tokenname'")

    sy = _Section(top.get("sync"), "sync")
    policy = sy.opt_str("ip_cleanup_policy", "delete_managed") or "delete_managed"
    if policy not in ("delete_managed", "unassign"):
        raise ConfigError("sync.ip_cleanup_policy must be 'delete_managed' or 'unassign'")
    sync = SyncConfig(
        source_id=sy.opt_str("source_id") or netbox.cluster,
        interface_name=sy.opt_str("interface_name", "eth0") or "eth0",
        include_templates=sy.opt_bool("include_templates", False),
        exclude_vmids=tuple(sy.opt_int_list("exclude_vmids")),
        exclude_pve_tags=tuple(t.lower() for t in sy.opt_str_list("exclude_pve_tags")),
        event_poll_interval=sy.opt_duration("event_poll_interval", "5s"),
        local_check_interval=sy.opt_duration("local_check_interval", "10m"),
        reconciliation_interval=sy.opt_duration("reconciliation_interval", "6h"),
        reconcile_on_startup=sy.opt_bool("reconcile_on_startup", True),
        adopt_existing=sy.opt_bool("adopt_existing", False),
        claim_unassigned_ips=sy.opt_bool("claim_unassigned_ips", True),
        ip_cleanup_policy=policy,
        max_deletions_per_run=sy.opt_int("max_deletions_per_run", 10),
        include_efi_tpm_disks=sy.opt_bool("include_efi_tpm_disks", False),
        sync_status=sy.opt_bool("sync_status", True),
    )
    sy.finish()
    if len(sync.source_id) > 100:
        raise ConfigError("sync.source_id is too long (max 100 characters)")

    rt = _Section(top.get("retry"), "retry")
    retry = RetryConfig(
        initial_delay=rt.opt_duration("initial_delay", "5s"),
        max_delay=rt.opt_duration("max_delay", "1h"),
        multiplier=rt.opt_float("multiplier", 2.0, minimum=1.0),
        jitter=rt.opt_float("jitter", 0.1),
    )
    rt.finish()
    if retry.max_delay < retry.initial_delay:
        raise ConfigError("retry.max_delay must be >= retry.initial_delay")
    if retry.jitter > 0.5:
        raise ConfigError("retry.jitter must be between 0 and 0.5")

    ga = _Section(top.get("guest_agent"), "guest_agent")
    guest_agent = GuestAgentConfig(
        ip_wait_initial_delay=ga.opt_duration("ip_wait_initial_delay", "30s"),
        ip_wait_max_delay=ga.opt_duration("ip_wait_max_delay", "15m"),
        ip_wait_timeout=ga.opt_duration("ip_wait_timeout", "24h"),
    )
    ga.finish()

    ips = _Section(top.get("ip_selection"), "ip_selection")
    ip_selection = IpSelectionConfig(
        exclude_interfaces=tuple(ips.opt_str_list("exclude_interfaces", DEFAULT_EXCLUDE_INTERFACES)),
        exclude_networks=_validate_networks(
            ips.opt_str_list("exclude_networks", DEFAULT_EXCLUDE_NETWORKS), "ip_selection.exclude_networks"),
        prefer_networks=_validate_networks(ips.opt_str_list("prefer_networks"), "ip_selection.prefer_networks"),
        prefer_interfaces=tuple(ips.opt_str_list("prefer_interfaces")),
        prefer_pve_mac=ips.opt_bool("prefer_pve_mac", True),
        require_pve_mac=ips.opt_bool("require_pve_mac", False),
    )
    ips.finish()

    lg = _Section(top.get("logging"), "logging")
    level = (lg.opt_str("level", "INFO") or "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise ConfigError("logging.level must be DEBUG, INFO, WARNING or ERROR")
    fmt = (lg.opt_str("format", "text") or "text").lower()
    if fmt not in ("text", "json"):
        raise ConfigError("logging.format must be 'text' or 'json'")
    logging_cfg = LoggingConfig(level=level, format=fmt)
    lg.finish()

    st = _Section(top.get("state"), "state")
    state_path = Path(st.opt_str("path", str(DEFAULT_STATE_PATH)) or str(DEFAULT_STATE_PATH))
    st.finish()

    top.get("secrets_file")  # consumed by load_config
    top.finish()
    return Config(netbox=netbox, proxmox=proxmox, sync=sync, retry=retry, guest_agent=guest_agent,
                  ip_selection=ip_selection, logging=logging_cfg, state_path=state_path,
                  source_path=source_path)


def load_config(path: Path | str | None = None, *, env: dict[str, str] | None = None) -> Config:
    """Load the YAML config file plus its ``secrets.env`` and return a validated :class:`Config`."""
    path = Path(path) if path else Path(os.environ.get("NETBOX_PVE_SYNC_CONFIG", DEFAULT_CONFIG_PATH))
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except PermissionError as exc:
        raise ConfigError(f"Cannot read configuration file {path}: permission denied") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    merged = dict(os.environ if env is None else env)
    secrets_file = None
    if isinstance(raw, dict) and raw.get("secrets_file"):
        secrets_file = Path(str(raw["secrets_file"]))
    else:
        secrets_file = path.parent / "secrets.env"
    for key, value in load_env_file(secrets_file).items():
        merged.setdefault(key, value)  # real environment variables win
    return parse_config(raw, env=merged, source_path=path)
