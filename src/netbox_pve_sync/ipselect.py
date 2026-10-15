"""Choose the VM's management IPv4 address from QEMU guest agent data.

The rules are explicit and fully driven by ``ip_selection`` in the config.
``netbox-pve-sync explain-ip --vmid N`` prints how they were applied.

1. Collect every address the guest agent reports.
2. **Reject** an address when any of these is true (the first that matches is the reason):
   * it is not IPv4 (IPv6 is never used for the management address);
   * it cannot be parsed;
   * its interface name matches ``exclude_interfaces`` (loopback, Docker,
     Kubernetes CNI, bridges, VPN tunnels, ... - shell-style globs);
   * it lies in ``exclude_networks`` (127/8, 169.254/16 link-local, 172.17/16
     Docker default bridge, multicast, ...);
   * it is unspecified, multicast or the broadcast address;
   * ``require_pve_mac`` is true and the interface's MAC is not one of the
     VM's Proxmox NICs (``netN``).
3. **Rank** the remaining candidates, best first:
   1. position in ``prefer_networks`` (first matching network wins);
   2. position in ``prefer_interfaces`` (glob match on the interface name);
   3. if ``prefer_pve_mac``: interfaces whose MAC belongs to a Proxmox NIC
      come first, in ``net0, net1, ...`` order. Guest-internal virtual
      interfaces (Docker, VPN, bridges) have MACs Proxmox does not know;
   4. the order in which the guest agent reported interfaces and addresses.
4. The best candidate is the management IPv4. If nothing is left, the VM has
   *no usable IP right now* - the existing NetBox IP is kept untouched.
"""

from __future__ import annotations

import fnmatch
import ipaddress
from dataclasses import dataclass, field
from typing import Any

from .config import IpSelectionConfig


@dataclass(frozen=True)
class IpCandidate:
    interface: str
    mac: str | None
    address: str      # "192.168.1.50"
    prefix: int       # 24

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"


@dataclass
class IpSelection:
    selected: IpCandidate | None
    candidates: list[IpCandidate] = field(default_factory=list)          # ranked, best first
    rejected: list[tuple[str, str, str]] = field(default_factory=list)   # (interface, address, reason)
    malformed: bool = False


def _glob_index(name: str, patterns: tuple[str, ...]) -> int | None:
    for index, pattern in enumerate(patterns):
        if fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(name.lower(), pattern.lower()):
            return index
    return None


def select_management_ipv4(interfaces: list[dict[str, Any]], pve_macs: list[str],
                           config: IpSelectionConfig) -> IpSelection:
    exclude_nets = [ipaddress.ip_network(n, strict=False) for n in config.exclude_networks]
    prefer_nets = [ipaddress.ip_network(n, strict=False) for n in config.prefer_networks]
    pve_macs = [m.lower() for m in pve_macs]
    rejected: list[tuple[str, str, str]] = []
    ranked: list[tuple[tuple[int, int, int, int, int], IpCandidate]] = []
    malformed = False

    for if_index, iface in enumerate(interfaces):
        name = str(iface.get("name") or f"if{if_index}")
        mac_raw = iface.get("hardware-address")
        mac = str(mac_raw).lower() if mac_raw else None
        addresses = iface.get("ip-addresses") or []
        if not isinstance(addresses, list):
            malformed = True
            rejected.append((name, "?", "malformed ip-addresses list"))
            continue
        for addr_index, entry in enumerate(addresses):
            if not isinstance(entry, dict):
                malformed = True
                rejected.append((name, "?", "malformed address entry"))
                continue
            raw_addr = str(entry.get("ip-address") or "").split("%")[0]
            family = str(entry.get("ip-address-type") or "").lower()
            if family != "ipv4":
                rejected.append((name, raw_addr, f"not IPv4 ({family or 'unknown'})"))
                continue
            try:
                ip = ipaddress.IPv4Address(raw_addr)
                prefix = int(entry.get("prefix", 32))
                if not 0 < prefix <= 32:
                    raise ValueError("bad prefix")
            except (ValueError, TypeError):
                malformed = True
                rejected.append((name, raw_addr, "malformed address or prefix"))
                continue
            if _glob_index(name, config.exclude_interfaces) is not None:
                rejected.append((name, raw_addr, "interface excluded by exclude_interfaces"))
                continue
            net_hit = next((n for n in exclude_nets if ip in n), None)
            if net_hit is not None:
                rejected.append((name, raw_addr, f"address in excluded network {net_hit}"))
                continue
            if ip.is_unspecified or ip.is_multicast or ip == ipaddress.IPv4Address("255.255.255.255"):
                rejected.append((name, raw_addr, "unspecified/multicast/broadcast address"))
                continue
            network = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
            if prefix < 31 and ip in (network.network_address, network.broadcast_address):
                rejected.append((name, raw_addr, "network or broadcast address of its subnet"))
                continue
            mac_rank = pve_macs.index(mac) if (mac and mac in pve_macs) else len(pve_macs)
            if config.require_pve_mac and mac_rank == len(pve_macs):
                rejected.append((name, raw_addr, "MAC is not one of the VM's Proxmox NICs (require_pve_mac)"))
                continue
            net_rank = next((i for i, n in enumerate(prefer_nets) if ip in n), len(prefer_nets))
            if_rank = _glob_index(name, config.prefer_interfaces)
            key = (
                net_rank,
                if_rank if if_rank is not None else len(config.prefer_interfaces),
                mac_rank if config.prefer_pve_mac else 0,
                if_index,
                addr_index,
            )
            ranked.append((key, IpCandidate(interface=name, mac=mac, address=str(ip), prefix=prefix)))

    ranked.sort(key=lambda item: item[0])
    candidates = [c for _, c in ranked]
    return IpSelection(selected=candidates[0] if candidates else None, candidates=candidates,
                       rejected=rejected, malformed=malformed)
