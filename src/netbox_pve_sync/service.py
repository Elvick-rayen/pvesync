"""Wires the clients together: startup checks, one-VM sync, queue processing and reconciliation."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .backoff import backoff_delay
from .config import Config, format_duration
from .discovery import IP_AGENT_DISABLED, IP_OK, DesiredVM, discover_vm, is_eligible
from .errors import (
    ApiError,
    AuthenticationError,
    ConflictError,
    FatalStartupError,
    NetBoxUnavailable,
    NotFoundError,
    ProxmoxUnavailable,
    TransientApiError,
)
from .logging_setup import get_logger, log, mask_secret
from .netbox.client import NetBoxClient
from .netbox.setup import NetBoxPrereqs, resolve_prerequisites
from .proxmox.client import ProxmoxClient, VmResource
from .state import StateStore
from .sync import Synchronizer, SyncResult

logger = get_logger("service")

NEVER = 1e18


@dataclass
class ReconcileReport:
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    conflicts: dict[int, str] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    skipped_deletions: list[int] = field(default_factory=list)
    ineligible: dict[int, str] = field(default_factory=dict)
    no_ip: dict[int, str] = field(default_factory=dict)
    results: list[SyncResult] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {"created": len(self.created), "updated": len(self.updated), "unchanged": len(self.unchanged),
                "deleted": len(self.deleted), "conflicts": len(self.conflicts), "errors": len(self.errors),
                "deletions_blocked": len(self.skipped_deletions)}


class SyncService:
    def __init__(self, config: Config, *, proxmox: ProxmoxClient | None = None,
                 netbox: NetBoxClient | None = None, state: StateStore | None = None,
                 dry_run: bool = False, clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.dry_run = dry_run
        self.clock = clock
        self.proxmox = proxmox or ProxmoxClient(config.proxmox)
        self.netbox = netbox or NetBoxClient(config.netbox, dry_run=dry_run)
        self.netbox.dry_run = dry_run
        self.state = state or StateStore(config.state_path, clock=clock)
        self.prereqs: NetBoxPrereqs | None = None
        self._sync: Synchronizer | None = None
        self.netbox_failures = 0
        self.netbox_down_until = 0.0

    # ================================================================ startup
    def check_proxmox(self) -> list[str]:
        """Connect to Proxmox, log the version and verify the token's privileges."""
        version = self.proxmox.version()
        compat = self.proxmox.compat
        warnings = compat.check_supported()
        info = self.proxmox.cluster_info()
        log(logger, logging.INFO, f"Connected to Proxmox VE {version.version}",
            pve_version=version.version, pve_major=compat.major, cluster=info.cluster_name or "(standalone)",
            node=info.local_node, api=self.config.proxmox.api_url, token_id=self.config.proxmox.token_id)
        if not self.config.proxmox.verify_ssl:
            log(logger, logging.WARNING, "TLS verification for the Proxmox API is DISABLED (proxmox.verify_ssl: false)")
        perms = self.proxmox.permissions()
        privs = {p for path in ("/", "/vms") for p, v in (perms.get(path) or {}).items() if v}
        missing = [p for p in compat.required_vm_privileges if p not in privs]
        if missing:
            raise FatalStartupError(
                f"Proxmox token {self.config.proxmox.token_id} lacks {', '.join(missing)} on /vms; "
                "re-run install.sh or see README 'Proxmox API token'")
        if not any(p in privs for p in compat.guest_agent_privileges):
            warnings.append(
                f"Proxmox token lacks {' or '.join(compat.guest_agent_privileges)} on /vms: IP addresses cannot be "
                f"read from the guest agent on PVE {compat.major}. Fix: pveum role modify NetBoxSync --privs "
                f"\"{','.join(compat.role_privileges)}\"")
        for w in warnings:
            log(logger, logging.WARNING, w)
        self.state.set("pve_version", version.version)
        return warnings

    def check_netbox(self) -> list[str]:
        status = self.netbox.status()
        compat = self.netbox.compat
        warnings = compat.check_supported()
        try:
            self.netbox.check_token()
        except AuthenticationError as exc:
            raise FatalStartupError(
                f"NetBox rejected the API token ({mask_secret(self.config.netbox.token)}): invalid, expired or "
                "without permission to view virtual machines") from exc
        self.prereqs = resolve_prerequisites(self.netbox, self.config.netbox.cluster, self.config.netbox.tag,
                                             self.config.netbox.vrf)
        self._sync = Synchronizer(self.netbox, self.config, self.prereqs)
        log(logger, logging.INFO, f"Connected to NetBox {compat.version}", netbox_version=str(compat.version),
            url=self.config.netbox.url, cluster=self.config.netbox.cluster, cluster_id=self.prereqs.cluster_id,
            source_id=self.config.sync.source_id, disk_unit=compat.disk_unit_name,
            token=mask_secret(self.config.netbox.token), rq_workers=status.get("rq-workers-running"))
        for w in warnings:
            log(logger, logging.WARNING, w)
        self.state.set("netbox_version", str(compat.version))
        return warnings

    @property
    def synchronizer(self) -> Synchronizer:
        if self._sync is None:
            self.check_netbox()
        assert self._sync is not None
        return self._sync

    # ================================================================ one VM
    def eligible_resources(self, resources: list[VmResource]) -> dict[int, VmResource]:
        return {vm.vmid: vm for vm in resources if is_eligible(vm, self.config)[0]}

    def sync_vmid(self, vmid: int, *, force: bool = False, resources: list[VmResource] | None = None,
                  existing: list[dict[str, Any]] | None = None, started: bool = False) -> SyncResult:
        """Make NetBox match the current Proxmox state of one VM (create, update or delete)."""
        if resources is None:
            resources = self.proxmox.list_vms()
        resource = next((vm for vm in resources if vm.vmid == vmid), None)
        eligible, why = is_eligible(resource, self.config) if resource else (False, "not in Proxmox")
        if resource is None and not resources:
            raise TransientApiError("Proxmox returned an empty VM list; refusing to treat that as a deletion")

        if not eligible:
            result = self.synchronizer.delete(vmid, existing=existing)
            if result.outcome == "deleted":
                log(logger, logging.INFO, f"VM {vmid} removed from NetBox", vmid=vmid, reason=why,
                    dry_run=self.dry_run or None)
            if not self.dry_run:
                self.state.forget_vm(vmid)
            return result

        assert resource is not None
        try:
            desired = discover_vm(self.proxmox, resource, self.config)
        except NotFoundError:
            # Deleted between the listing and the config read: let the next round see it gone.
            raise TransientApiError(f"VM {vmid} vanished while being read; will re-check") from None

        self._log_ip_state(desired)
        if not force and not self.dry_run and self.state.fingerprint(vmid) == desired.fingerprint():
            self._update_ip_wait(desired, started=started)
            return SyncResult(vmid=vmid, outcome="unchanged", ip_status=desired.ip_status)

        result = self.synchronizer.sync(desired, existing=existing)
        if result.outcome in ("created", "updated"):
            log(logger, logging.INFO, f"VM {vmid} synchronized successfully", vmid=vmid, name=desired.name,
                outcome=result.outcome, changes=result.changes, dry_run=self.dry_run or None)
        else:
            log(logger, logging.DEBUG, f"VM {vmid} already up to date", vmid=vmid)
        for warning in result.warnings:
            log(logger, logging.WARNING, warning, vmid=vmid)
        if not self.dry_run:
            if not result.conflict:
                self.state.record_synced(vmid, desired.fingerprint(), netbox_id=result.netbox_id,
                                         name=desired.name, ip=desired.ip.cidr if desired.ip else None,
                                         ip_status=desired.ip_status)
            self._update_ip_wait(desired, started=started)
        if result.conflict:
            raise ConflictError("; ".join(result.warnings))
        return result

    def _log_ip_state(self, desired: DesiredVM) -> None:
        if desired.ip_status == IP_OK:
            return
        level = logging.WARNING if desired.waiting_for_ip else logging.INFO
        if desired.ip_status == IP_AGENT_DISABLED:
            level = logging.DEBUG
        log(logger, level, f"QEMU Guest Agent unavailable for VM {desired.vmid}; keeping the last known NetBox IP"
            if desired.waiting_for_ip else f"No management IP for VM {desired.vmid}",
            vmid=desired.vmid, status=desired.ip_status, detail=desired.ip_detail)

    def _update_ip_wait(self, desired: DesiredVM, *, started: bool) -> None:
        """Schedule fast re-checks for a running VM whose IP is not known yet (backoff up to ip_wait_max_delay)."""
        if not desired.waiting_for_ip:
            self.state.ip_wait_clear(desired.vmid)
            return
        ga = self.config.guest_agent
        current = self.state.ip_wait_get(desired.vmid)
        if current is not None and started:
            self.state.ip_wait_clear(desired.vmid)  # a fresh boot restarts the fast checks
            current = None
        now = self.clock()
        if current is not None and now - current.since > ga.ip_wait_timeout:
            if current.next_check < NEVER:
                log(logger, logging.WARNING, f"Still no usable IP for VM {desired.vmid} after "
                    f"{format_duration(ga.ip_wait_timeout)}; only the periodic check will look again",
                    vmid=desired.vmid, reason=desired.ip_status)
            self.state.ip_wait_set(desired.vmid, next_check=NEVER, reason=desired.ip_status)
            return
        attempts = (current.attempts + 1) if current else 1
        delay = backoff_delay(attempts, initial=ga.ip_wait_initial_delay, maximum=ga.ip_wait_max_delay,
                              multiplier=2.0, jitter=self.config.retry.jitter)
        self.state.ip_wait_set(desired.vmid, next_check=now + delay, reason=desired.ip_status)
        log(logger, logging.DEBUG, "Will re-check guest IP", vmid=desired.vmid, in_seconds=round(delay))

    # ================================================================ queue
    def process_queue(self, *, stop: Callable[[], bool] = lambda: False, limit: int = 50) -> int:
        """Run due queue items. Returns how many were completed."""
        now = self.clock()
        if now < self.netbox_down_until:
            return 0
        done = 0
        resources: list[VmResource] | None = None
        for item in self.state.due(limit):
            if stop():
                break
            try:
                if resources is None:
                    resources = self.proxmox.list_vms()
                started = "started" in item.reason
                self.sync_vmid(item.vmid, resources=resources, started=started)
            except NetBoxUnavailable as exc:
                self.netbox_failures += 1
                delay = self._retry_delay(self.netbox_failures)
                self.netbox_down_until = self.clock() + delay
                self.state.postpone_all(self.netbox_down_until)
                self.state.defer(item.vmid, delay, str(exc))
                log(logger, logging.WARNING, "NetBox unavailable; synchronization queued",
                    queued=len(self.state.queue_items()), retry_in=format_duration(round(delay)),
                    attempt=self.netbox_failures, error=str(exc))
                break
            except ProxmoxUnavailable as exc:
                attempts = self.state.defer(item.vmid, self._retry_delay(item.attempts + 1), str(exc))
                log(logger, logging.WARNING, "Proxmox API unavailable; will retry", vmid=item.vmid,
                    attempt=attempts, error=str(exc))
                break
            except ConflictError as exc:
                delay = self._retry_delay(item.attempts + 1, slow=True)
                attempts = self.state.defer(item.vmid, delay, str(exc))
                log(logger, logging.ERROR, f"Failed to synchronize VM {item.vmid}: conflict needs attention",
                    vmid=item.vmid, attempt=attempts, retry_in=format_duration(round(delay)), error=str(exc))
            except TransientApiError as exc:
                delay = self._retry_delay(item.attempts + 1)
                attempts = self.state.defer(item.vmid, delay, str(exc))
                log(logger, logging.WARNING, f"Temporary problem synchronizing VM {item.vmid}; retrying",
                    vmid=item.vmid, attempt=attempts, retry_in=format_duration(round(delay)), error=str(exc))
            except (ApiError, FatalStartupError) as exc:
                delay = self._retry_delay(item.attempts + 1, slow=True)
                attempts = self.state.defer(item.vmid, delay, str(exc))
                log(logger, logging.ERROR, f"Failed to synchronize VM {item.vmid}", vmid=item.vmid,
                    attempt=attempts, retry_in=format_duration(round(delay)), error=str(exc))
            except Exception as exc:  # one broken VM must never stop the others
                delay = self._retry_delay(item.attempts + 1, slow=True)
                attempts = self.state.defer(item.vmid, delay, f"{exc.__class__.__name__}: {exc}")
                log(logger, logging.ERROR, f"Failed to synchronize VM {item.vmid} (unexpected error)",
                    vmid=item.vmid, attempt=attempts, exc_info=True)
            else:
                if item.attempts:
                    log(logger, logging.INFO, f"VM {item.vmid} synchronized after retry", vmid=item.vmid,
                        attempts=item.attempts)
                self.state.complete(item.vmid)
                self.netbox_failures = 0
                done += 1
        return done

    def _retry_delay(self, attempt: int, *, slow: bool = False) -> float:
        r = self.config.retry
        # Errors that need a human (conflicts, validation) start at 1 minute instead of 5 seconds.
        initial = max(r.initial_delay, 60.0) if slow else r.initial_delay
        return backoff_delay(attempt, initial=min(initial, r.max_delay), maximum=r.max_delay,
                             multiplier=r.multiplier, jitter=r.jitter)

    # ================================================================ reconciliation
    def reconcile(self, *, allow_mass_delete: bool = False) -> ReconcileReport:
        """Compare every eligible Proxmox VM with NetBox and repair all differences."""
        report = ReconcileReport()
        started = self.clock()
        resources = self.proxmox.list_vms()
        eligible = self.eligible_resources(resources)
        for vm in resources:
            ok, why = is_eligible(vm, self.config)
            if not ok:
                report.ineligible[vm.vmid] = why
        managed = self.synchronizer.managed_vms()
        log(logger, logging.INFO, "Reconciliation started", proxmox_vms=len(resources), eligible=len(eligible),
            netbox_managed=len(managed), dry_run=self.dry_run or None)

        for vmid in sorted(eligible):
            try:
                result = self.sync_vmid(vmid, force=True, resources=resources, existing=managed.get(vmid, []))
                report.results.append(result)
                bucket = {"created": report.created, "updated": report.updated}.get(result.outcome, report.unchanged)
                bucket.append(vmid)
                if result.ip_status != IP_OK:
                    report.no_ip[vmid] = result.ip_status
                if not self.dry_run:
                    self.state.complete(vmid)
            except ConflictError as exc:
                report.conflicts[vmid] = str(exc)
                self._requeue(vmid, f"reconcile conflict: {exc}", slow=True)
            except NetBoxUnavailable:
                raise
            except Exception as exc:  # keep going with the other VMs
                report.errors[vmid] = f"{exc.__class__.__name__}: {exc}"
                log(logger, logging.ERROR, f"Failed to synchronize VM {vmid} during reconciliation",
                    vmid=vmid, error=str(exc), exc_info=not isinstance(exc, ApiError))
                self._requeue(vmid, "reconcile error", slow=True)

        to_delete = sorted(set(managed) - set(eligible))
        if to_delete and not resources:
            log(logger, logging.ERROR, "Proxmox returned no VMs at all; refusing to delete NetBox VMs",
                would_delete=len(to_delete))
            report.skipped_deletions = to_delete
            to_delete = []
        limit = self.config.sync.max_deletions_per_run
        if len(to_delete) > limit and not allow_mass_delete:
            log(logger, logging.ERROR, f"Reconciliation would delete {len(to_delete)} NetBox VMs (limit "
                f"{limit}); nothing deleted. Check the config, then run 'netbox-pve-sync reconcile "
                "--allow-mass-delete' if this is intended", vmids=to_delete)
            report.skipped_deletions = to_delete
            to_delete = []
        for vmid in to_delete:
            try:
                result = self.synchronizer.delete(vmid, existing=managed[vmid])
                report.results.append(result)
                report.deleted.append(vmid)
                log(logger, logging.INFO, f"VM {vmid} removed from NetBox", vmid=vmid,
                    reason=report.ineligible.get(vmid, "not in Proxmox"), dry_run=self.dry_run or None)
                if not self.dry_run:
                    self.state.forget_vm(vmid)
                    self.state.complete(vmid)
            except NetBoxUnavailable:
                raise
            except Exception as exc:
                report.errors[vmid] = f"{exc.__class__.__name__}: {exc}"
                log(logger, logging.ERROR, f"Failed to delete VM {vmid} from NetBox", vmid=vmid, error=str(exc))

        if not self.dry_run:
            self.state.set("last_reconcile", str(self.clock()))
            self.state.set("last_reconcile_summary", repr(report.summary()))
        log(logger, logging.INFO, "Reconciliation finished", duration=f"{self.clock() - started:.1f}s",
            dry_run=self.dry_run or None, **report.summary())  # type: ignore[arg-type]
        return report

    def _requeue(self, vmid: int, reason: str, *, slow: bool) -> None:
        if self.dry_run:
            return
        self.state.enqueue(vmid, reason, delay=self._retry_delay(1, slow=slow))
