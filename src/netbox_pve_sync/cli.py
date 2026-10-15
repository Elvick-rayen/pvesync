"""Command line interface: ``netbox-pve-sync <command>``."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import Config, format_duration, load_config
from .daemon import EXIT_CONFIG, Daemon
from .discovery import discover_vm, is_eligible
from .errors import ApiError, AuthenticationError, ConfigError, FatalStartupError, SyncError, TransientApiError
from .logging_setup import mask_secret, setup_logging
from .netbox.client import NetBoxClient
from .netbox.setup import ensure_prerequisites, missing_prerequisites
from .proxmox.client import ProxmoxClient
from .service import ReconcileReport, SyncService
from .state import StateStore

EXIT_ERROR = 1


def _ts(value: float | None) -> str:
    if value is None:
        return "never"
    if value >= 1e17:
        return "not scheduled"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def _print_planned(service: SyncService) -> None:
    planned = service.netbox.planned
    print()
    if not planned:
        print("DRY RUN: NetBox is already up to date - no changes would be made.")
        return
    print(f"DRY RUN: {len(planned)} NetBox API write(s) would be made (nothing was changed):")
    for change in planned:
        print(f"  {change.action.upper():<7} {change.description}")


def _print_report(report: ReconcileReport, dry_run: bool) -> None:
    print()
    print("Reconciliation " + ("plan (nothing was changed)" if dry_run else "result") + ":")
    for result in report.results:
        if result.changes:
            prefix = "would be " if dry_run else ""
            print(f"  VM {result.vmid}: {prefix}{result.outcome} - " + "; ".join(result.changes))
    print(f"  created: {len(report.created)}   updated: {len(report.updated)}   "
          f"unchanged: {len(report.unchanged)}   deleted: {len(report.deleted)}")
    for vmid, why in sorted(report.ineligible.items()):
        print(f"  skipped VM {vmid}: {why}")
    for vmid, status in sorted(report.no_ip.items()):
        print(f"  no management IP for VM {vmid}: {status} (existing NetBox IP is kept)")
    for vmid, msg in sorted(report.conflicts.items()):
        print(f"  CONFLICT VM {vmid}: {msg}")
    for vmid, msg in sorted(report.errors.items()):
        print(f"  ERROR VM {vmid}: {msg}")
    if report.skipped_deletions:
        print(f"  BLOCKED deletion of {len(report.skipped_deletions)} VM(s) {report.skipped_deletions} "
              "(safety limit; see log)")


def _service(config: Config, dry_run: bool) -> SyncService:
    return SyncService(config, dry_run=dry_run)


# ------------------------------------------------------------------ commands
def cmd_run(config: Config, args: argparse.Namespace) -> int:
    daemon = Daemon(config)
    daemon.install_signal_handlers()
    return daemon.run()


def cmd_test_connection(config: Config, args: argparse.Namespace) -> int:
    ok = True
    print(f"Configuration: {config.source_path}")
    print(f"  NetBox URL:      {config.netbox.url}")
    print(f"  NetBox token:    {mask_secret(config.netbox.token)}")
    print(f"  NetBox cluster:  {config.netbox.cluster}")
    print(f"  Source id:       {config.sync.source_id}")
    print(f"  Proxmox API:     {config.proxmox.api_url} (verify TLS: {config.proxmox.verify_ssl})")
    print(f"  Proxmox token:   {config.proxmox.token_id} / {mask_secret(config.proxmox.token_secret)}")
    print()
    px = ProxmoxClient(config.proxmox)
    try:
        version = px.version()
        info = px.cluster_info()
        vms = px.list_vms()
        print(f"[ OK ] Proxmox VE {version.version} - cluster {info.cluster_name or '(standalone)'}, "
              f"node {info.local_node}, {len(vms)} QEMU VM(s) visible")
        perms = px.permissions()
        privs = {p for path in ("/", "/vms") for p, v in (perms.get(path) or {}).items() if v}
        need = list(px.compat.required_vm_privileges)
        ga = px.compat.guest_agent_privileges
        missing = [p for p in need if p not in privs]
        if missing:
            ok = False
            print(f"[FAIL] Proxmox token lacks {', '.join(missing)} on /vms")
        elif not any(p in privs for p in ga):
            print(f"[WARN] Proxmox token lacks {' or '.join(ga)}: guest IPs cannot be read")
        else:
            print(f"[ OK ] Proxmox token privileges: {', '.join(sorted(privs & {*need, *ga, 'Sys.Audit'}))}")
    except (SyncError, ApiError) as exc:
        ok = False
        print(f"[FAIL] Proxmox: {exc}")

    nb = NetBoxClient(config.netbox)
    try:
        nb.status()
        print(f"[ OK ] NetBox {nb.compat.version} reachable (VM disk unit: {nb.compat.disk_unit_name})")
        for warning in nb.compat.check_supported():
            print(f"[WARN] {warning}")
        nb.check_token()
        print("[ OK ] NetBox token accepted")
        cluster = nb.cluster(config.netbox.cluster)
        if cluster is None:
            ok = False
            print(f"[FAIL] NetBox cluster '{config.netbox.cluster}' does not exist (create it in NetBox first)")
        else:
            print(f"[ OK ] NetBox cluster '{config.netbox.cluster}' exists (id {cluster['id']})")
        problems = missing_prerequisites(nb, config.netbox.tag)
        if problems:
            ok = False
            for problem in problems:
                print(f"[FAIL] NetBox: {problem}  ->  run 'netbox-pve-sync init-netbox'")
        else:
            print("[ OK ] NetBox custom fields and tag present")
    except (SyncError, ApiError) as exc:
        ok = False
        print(f"[FAIL] NetBox: {exc}")
    print()
    print("All checks passed." if ok else "Some checks FAILED - see above.")
    return 0 if ok else EXIT_ERROR


def cmd_init_netbox(config: Config, args: argparse.Namespace) -> int:
    nb = NetBoxClient(config.netbox, dry_run=args.dry_run)
    print(f"NetBox {nb.compat.version} at {config.netbox.url}")
    actions = ensure_prerequisites(nb, config.netbox.tag)
    if args.dry_run:
        for change in nb.planned:
            print(f"  would {change.action} {change.description}")
        if not nb.planned:
            print("  nothing to do")
    else:
        for action in actions or ["nothing to do - custom fields and tag already exist"]:
            print(f"  {action}")
    if nb.cluster(config.netbox.cluster) is None:
        print(f"\nNOTE: cluster '{config.netbox.cluster}' does not exist yet. Create it in NetBox "
              "(Virtualization > Clusters); the agent never creates clusters.")
        return EXIT_ERROR
    return 0


def cmd_reconcile(config: Config, args: argparse.Namespace) -> int:
    service = _service(config, args.dry_run)
    service.check_proxmox()
    service.check_netbox()
    report = service.reconcile(allow_mass_delete=args.allow_mass_delete)
    _print_report(report, args.dry_run)
    if args.dry_run:
        _print_planned(service)
    return EXIT_ERROR if (report.errors or report.conflicts or report.skipped_deletions) else 0


def cmd_sync(config: Config, args: argparse.Namespace) -> int:
    service = _service(config, args.dry_run)
    service.check_proxmox()
    service.check_netbox()
    rc = 0
    for vmid in args.vmid:
        try:
            result = service.sync_vmid(vmid, force=True)
            changes = "; ".join(result.changes) or "no changes"
            print(f"VM {vmid}: {result.outcome} - {changes}")
            if not args.dry_run:
                service.state.complete(vmid)
        except SyncError as exc:
            rc = EXIT_ERROR
            print(f"VM {vmid}: FAILED - {exc}")
    if args.dry_run:
        _print_planned(service)
    return rc


def cmd_explain_ip(config: Config, args: argparse.Namespace) -> int:
    px = ProxmoxClient(config.proxmox)
    resource = next((vm for vm in px.list_vms() if vm.vmid == args.vmid), None)
    if resource is None:
        print(f"VM {args.vmid} not found in Proxmox")
        return EXIT_ERROR
    eligible, why = is_eligible(resource, config)
    desired = discover_vm(px, resource, config)
    suffix = f" ({why})" if why else ""
    print(f"VM {desired.vmid} '{desired.name}' status={desired.status} eligible={eligible}{suffix}")
    print(f"  memory={desired.memory_mb} MB  vcpus={desired.vcpus:g}  disk={desired.disk_bytes / 1024**3:.1f} GiB")
    print(f"  IP status: {desired.ip_status} {desired.ip_detail}")
    sel = desired.selection
    if sel is None:
        return 0
    print("  Candidates (best first):")
    for i, c in enumerate(sel.candidates):
        print(f"    {'*' if i == 0 else ' '} {c.cidr:<20} iface={c.interface} mac={c.mac}")
    if not sel.candidates:
        print("    (none)")
    print("  Rejected:")
    for iface, addr, reason in sel.rejected:
        print(f"      {addr:<28} iface={iface}: {reason}")
    print(f"  => Management IPv4: {desired.ip.cidr if desired.ip else 'none (NetBox IP left unchanged)'}")
    return 0


def _systemd_state() -> str:
    if not shutil.which("systemctl"):
        return "unknown (no systemctl)"
    try:
        out = subprocess.run(["systemctl", "is-active", "netbox-pve-sync.service"], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    print(f"netbox-pve-sync {__version__}")
    print(f"  service:          {_systemd_state()}")
    print(f"  config:           {config.source_path}")
    print(f"  NetBox:           {config.netbox.url}  cluster={config.netbox.cluster}  source={config.sync.source_id}")
    print(f"  Proxmox:          {config.proxmox.api_url}")
    if not Path(config.state_path).exists():
        print(f"  state:            {config.state_path} (not created yet - the agent has not run)")
        return 0
    state = StateStore(config.state_path)
    heartbeat = state.get_float("heartbeat")
    print(f"  state:            {config.state_path}")
    print(f"  versions seen:    PVE {state.get('pve_version') or '?'}, NetBox {state.get('netbox_version') or '?'}")
    role = {"1": "active", "0": "standby"}.get(state.get("active") or "", "?")
    print(f"  role:             {role}")
    print(f"  last heartbeat:   {_ts(heartbeat)}")
    print(f"  last reconcile:   {_ts(state.get_float('last_reconcile'))}  {state.get('last_reconcile_summary') or ''}")
    print(f"  synced VMs:       {len(state.vm_states())}")
    items = state.queue_items()
    print(f"  retry queue:      {len(items)} item(s)")
    for item in items:
        err = f"  last error: {item.last_error}" if item.last_error else ""
        print(f"    VM {item.vmid:<6} attempts={item.attempts:<3} next={_ts(item.next_attempt)}  "
              f"reason={item.reason}{err}")
    waits = state.ip_waits()
    if waits:
        print(f"  waiting for IP:   {len(waits)} VM(s)")
        for w in waits:
            print(f"    VM {w.vmid:<6} since={_ts(w.since)} checks={w.attempts} next={_ts(w.next_check)} "
                  f"({w.last_reason})")
    if args.verbose:
        print("  synced VM state:")
        for row in state.vm_states():
            print(f"    VM {row['vmid']:<6} {row['name'] or '':<30} ip={row['ip'] or '-':<20} "
                  f"ip_status={row['ip_status']} synced={_ts(row['last_synced'])}")
    state.close()
    return 0


def cmd_queue(config: Config, args: argparse.Namespace) -> int:
    state = StateStore(config.state_path)
    if args.retry_now:
        print(f"{state.retry_now()} queued item(s) will be retried on the agent's next pass")
    elif args.clear:
        for item in state.queue_items():
            state.complete(item.vmid)
        print("Queue cleared (the next reconciliation will catch anything still out of sync)")
    else:
        for item in state.queue_items():
            print(f"VM {item.vmid}: attempts={item.attempts} next={_ts(item.next_attempt)} reason={item.reason} "
                  f"error={item.last_error or '-'}")
    state.close()
    return 0


def cmd_check_config(config: Config, args: argparse.Namespace) -> int:
    print(f"Configuration OK: {config.source_path}")
    print(f"  netbox.cluster={config.netbox.cluster} sync.source_id={config.sync.source_id} "
          f"interface={config.sync.interface_name}")
    print(f"  intervals: events={format_duration(config.sync.event_poll_interval)} "
          f"local_check={format_duration(config.sync.local_check_interval)} "
          f"reconciliation={format_duration(config.sync.reconciliation_interval)}")
    print(f"  retry: {format_duration(config.retry.initial_delay)} .. {format_duration(config.retry.max_delay)}")
    print(f"  ip_cleanup_policy={config.sync.ip_cleanup_policy} adopt_existing={config.sync.adopt_existing}")
    if not config.proxmox.verify_ssl or not config.netbox.verify_ssl:
        off = [n for n, c in (("Proxmox", config.proxmox), ("NetBox", config.netbox)) if not c.verify_ssl]
        print("  WARNING: TLS verification is disabled for " + " and ".join(off))
    if config.netbox.url.startswith("http://"):
        print("  NOTE: NetBox is reached over plain HTTP; the API token travels unencrypted on the LAN")
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netbox-pve-sync",
        description="Synchronize Proxmox VE virtual machines into NetBox (one-way, Proxmox is the source of truth).",
        epilog="Run without a command and with --dry-run to preview a full reconciliation.")
    parser.add_argument("-c", "--config", help="config file (default /etc/netbox-pve-sync/config.yaml)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="override logging.level")
    parser.add_argument("--dry-run", action="store_true", help="show what would change; never write to NetBox")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="run the synchronization agent (used by systemd)")
    sub.add_parser("version", help="print the version")
    sub.add_parser("check-config", help="validate the configuration file")
    sub.add_parser("test-connection", help="check Proxmox and NetBox connectivity, tokens and prerequisites")
    p = sub.add_parser("status", help="show agent state, retry queue and VMs waiting for an IP")
    p.add_argument("-v", "--verbose", action="store_true", help="also list every synced VM")
    p = sub.add_parser("reconcile", help="full comparison Proxmox -> NetBox (also the initial synchronization)")
    p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    p.add_argument("--allow-mass-delete", action="store_true",
                   help="allow deleting more than sync.max_deletions_per_run NetBox VMs")
    p = sub.add_parser("sync", help="synchronize specific VMs now")
    p.add_argument("--vmid", type=int, action="append", required=True, help="Proxmox VMID (repeatable)")
    p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    p = sub.add_parser("init-netbox", help="create the custom fields and tag this agent needs in NetBox")
    p.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    p = sub.add_parser("explain-ip", help="show how the management IPv4 of a VM is chosen")
    p.add_argument("--vmid", type=int, required=True)
    p = sub.add_parser("queue", help="list, retry or clear the retry queue")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--retry-now", action="store_true", help="make every queued item due immediately")
    group.add_argument("--clear", action="store_true", help="drop all queued items")
    return parser


COMMANDS = {
    "run": cmd_run, "status": cmd_status, "test-connection": cmd_test_connection, "reconcile": cmd_reconcile,
    "sync": cmd_sync, "init-netbox": cmd_init_netbox, "explain-ip": cmd_explain_ip, "queue": cmd_queue,
    "check-config": cmd_check_config,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(f"netbox-pve-sync {__version__}")
        return 0
    if args.command is None:
        if not args.dry_run:
            parser.print_help()
            return 0
        args.command = "reconcile"       # `netbox-pve-sync --dry-run` = preview a full reconciliation
        args.allow_mass_delete = False
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    setup_logging(args.log_level or config.logging.level, config.logging.format, config.secrets())
    if args.dry_run and args.command not in ("reconcile", "sync", "init-netbox"):
        print(f"--dry-run is not supported for '{args.command}'", file=sys.stderr)
        return EXIT_ERROR
    try:
        return COMMANDS[args.command](config, args)
    except (FatalStartupError, AuthenticationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except TransientApiError as exc:
        print(f"ERROR (temporary, try again later): {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


def entrypoint() -> None:
    logging.captureWarnings(True)
    sys.exit(main())
