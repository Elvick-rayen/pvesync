"""The long-running agent (``netbox-pve-sync run``, started by systemd).

One thread, one loop. Each pass does whatever is due, then sleeps until the
next deadline (or until SIGTERM/SIGINT):

* every ``event_poll_interval`` (5 s): detect Proxmox events -> queue those VMIDs
* every ``local_check_interval`` (10 m): queue every eligible VM for a cheap
  Proxmox-only check (catches guest IP changes, which Proxmox has no event for;
  NetBox is only contacted if something actually changed)
* when due: re-check VMs that are waiting for their guest agent to report an IP
* every ``reconciliation_interval`` (6 h) and at startup: full reconciliation
* always: run due items of the persistent retry queue

In a multi-node cluster the agent may be installed on every node; only the
online node with the lowest node ID is *active*, the others stand by and take
over automatically (see :mod:`leader`).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from types import FrameType

from . import __version__
from .backoff import backoff_delay
from .config import Config, format_duration
from .errors import AuthenticationError, FatalStartupError, NetBoxUnavailable, TransientApiError
from .leader import LeaderElector
from .logging_setup import get_logger, log
from .proxmox.events import EventDetector
from .service import SyncService

logger = get_logger("daemon")

EXIT_OK = 0
EXIT_CONFIG = 78  # EX_CONFIG: systemd unit has RestartPreventExitStatus=78
LEADER_CHECK_INTERVAL = 30.0


class Daemon:
    def __init__(self, config: Config, service: SyncService | None = None, *,
                 clock: Callable[[], float] = time.time) -> None:
        self.config = config
        self.clock = clock
        self.service = service or SyncService(config, clock=clock)
        self.state = self.service.state
        self.detector = EventDetector(self.service.proxmox, self.state)
        self.leader = LeaderElector(self.service.proxmox, enabled=config.proxmox.leader_election,
                                    agent_nodes=config.proxmox.agent_nodes)
        self.stop_event = threading.Event()
        self.next_event_poll = 0.0
        self.next_local_check = 0.0
        self.next_leader_check = 0.0
        self.next_reconcile = 0.0
        self.poll_failures = 0
        self.reconcile_failures = 0

    # ------------------------------------------------------------- signals
    def install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: FrameType | None) -> None:
            log(logger, logging.INFO, f"Received {signal.Signals(signum).name}; stopping after the current step")
            self.stop_event.set()
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def stopped(self) -> bool:
        return self.stop_event.is_set()

    # ------------------------------------------------------------- startup
    def startup(self) -> bool:
        """Connect and verify both sides; retry transient failures with backoff. False = stop requested."""
        attempt = 0
        while not self.stopped():
            attempt += 1
            try:
                self.service.check_proxmox()
                self.service.check_netbox()
                return True
            except (FatalStartupError, AuthenticationError):
                raise
            except TransientApiError as exc:
                delay = backoff_delay(attempt, initial=self.config.retry.initial_delay,
                                      maximum=min(self.config.retry.max_delay, 300.0))
                log(logger, logging.WARNING, "Startup check failed; retrying", attempt=attempt,
                    retry_in=format_duration(round(delay)), error=str(exc))
                self.stop_event.wait(delay)
        return False

    def schedule_initial(self) -> None:
        now = self.clock()
        last = self.state.get_float("last_reconcile")
        if self.config.sync.reconcile_on_startup or last is None:
            self.next_reconcile = now
        else:
            self.next_reconcile = max(now, last + self.config.sync.reconciliation_interval)
        self.next_local_check = now + self.config.sync.local_check_interval

    # ------------------------------------------------------------- one pass
    def step(self) -> float:
        """Do everything that is due. Returns the time of the next deadline."""
        now = self.clock()
        if now >= self.next_leader_check:
            self.next_leader_check = now + LEADER_CHECK_INTERVAL
            became_active = self.leader.check()
            self.state.set("active", "1" if self.leader.active else "0")
            if became_active:
                self.next_reconcile = now  # we may have missed changes while standing by
        if not self.leader.active:
            return self.next_leader_check

        if now >= self.next_event_poll:
            self._poll_events()
        if self.stopped():
            return now
        if now >= self.next_local_check:
            self._queue_local_check()
        for wait in self.state.ip_waits_due():
            self.state.enqueue(wait.vmid, "waiting for guest IP", urgent=False)
            self.state.ip_wait_touch(wait.vmid, now + self.config.guest_agent.ip_wait_max_delay)
        if now >= self.next_reconcile and now >= self.service.netbox_down_until:
            self._reconcile()
        if self.stopped():
            return now
        self.service.process_queue(stop=self.stopped)
        self.state.set("heartbeat", str(self.clock()))

        deadlines = [self.next_event_poll, self.next_local_check, self.next_reconcile, self.next_leader_check]
        for extra in (self.state.next_queue_time(), self.state.next_ip_wait_time()):
            if extra is not None:
                deadlines.append(max(extra, self.service.netbox_down_until))
        return min(deadlines)

    def _poll_events(self) -> None:
        now = self.clock()
        try:
            events, _ = self.detector.poll()
        except TransientApiError as exc:
            self.poll_failures += 1
            delay = backoff_delay(self.poll_failures, initial=self.config.sync.event_poll_interval,
                                  maximum=min(self.config.retry.max_delay, 300.0))
            self.next_event_poll = now + delay
            log(logger, logging.WARNING, "Cannot read Proxmox events; retrying", attempt=self.poll_failures,
                retry_in=format_duration(round(delay)), error=str(exc))
            return
        except AuthenticationError as exc:
            self.poll_failures += 1
            self.next_event_poll = now + 60
            log(logger, logging.ERROR, "Proxmox rejected the API token while reading events", error=str(exc))
            return
        if self.poll_failures:
            log(logger, logging.INFO, "Proxmox API reachable again")
        self.poll_failures = 0
        self.next_event_poll = now + self.config.sync.event_poll_interval
        for ev in events:
            log(logger, logging.INFO, f"Proxmox event for VM {ev.vmid}", vmid=ev.vmid, kind=ev.kind, detail=ev.detail)
            self.state.enqueue(ev.vmid, "started" if ev.started else ev.kind, urgent=True)

    def _queue_local_check(self) -> None:
        now = self.clock()
        self.next_local_check = now + self.config.sync.local_check_interval
        try:
            resources = self.service.proxmox.list_vms()
        except (TransientApiError, AuthenticationError) as exc:
            log(logger, logging.WARNING, "Periodic local check skipped: Proxmox unavailable", error=str(exc))
            return
        eligible = self.service.eligible_resources(resources)
        for vmid in eligible:
            self.state.enqueue(vmid, "periodic check", urgent=False)
        log(logger, logging.DEBUG, "Periodic local check queued", vms=len(eligible))

    def _reconcile(self) -> None:
        now = self.clock()
        try:
            self.service.reconcile()
            self.reconcile_failures = 0
            self.next_reconcile = self.clock() + self.config.sync.reconciliation_interval
        except (NetBoxUnavailable, TransientApiError, AuthenticationError) as exc:
            self.reconcile_failures += 1
            delay = backoff_delay(self.reconcile_failures, initial=60.0, maximum=self.config.retry.max_delay)
            self.next_reconcile = now + delay
            log(logger, logging.WARNING, "Reconciliation postponed", retry_in=format_duration(round(delay)),
                error=str(exc))
        except Exception:
            self.reconcile_failures += 1
            self.next_reconcile = now + self.config.retry.max_delay
            log(logger, logging.ERROR, "Reconciliation failed unexpectedly", exc_info=True)

    # ------------------------------------------------------------- main loop
    def run(self) -> int:
        log(logger, logging.INFO, f"Agent started (netbox-pve-sync {__version__})", cluster=self.config.netbox.cluster,
            source_id=self.config.sync.source_id, config=str(self.config.source_path or ""),
            state=str(self.config.state_path))
        try:
            if not self.startup():
                return EXIT_OK
        except (FatalStartupError, AuthenticationError) as exc:
            log(logger, logging.ERROR, f"Startup failed: {exc}")
            return EXIT_CONFIG
        self.schedule_initial()
        log(logger, logging.INFO, "Watching Proxmox for VM changes",
            event_poll=format_duration(self.config.sync.event_poll_interval),
            local_check=format_duration(self.config.sync.local_check_interval),
            reconciliation=format_duration(self.config.sync.reconciliation_interval),
            queued=len(self.state.queue_items()))
        while not self.stopped():
            try:
                next_deadline = self.step()
            except Exception:
                log(logger, logging.ERROR, "Unexpected error in main loop; continuing", exc_info=True)
                next_deadline = self.clock() + 30
            self.stop_event.wait(max(0.2, min(next_deadline - self.clock(), 60.0)))
        log(logger, logging.INFO, "Agent stopped", queued=len(self.state.queue_items()))
        self.state.close()
        return EXIT_OK
