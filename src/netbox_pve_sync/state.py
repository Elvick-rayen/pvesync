"""Small local SQLite state (default ``/var/lib/netbox-pve-sync/state.db``).

NetBox remains the inventory; this file only holds what the agent needs to
survive restarts without losing work. It can be deleted at any time - the next
reconciliation rebuilds everything (only the retry history is lost).

Tables
------
``queue``              VMIDs that still need a sync, with attempt count, next
                       attempt time and last error. One row per VMID, so a
                       burst of duplicate events collapses into one sync.
``ip_wait``            Running VMs whose guest agent has not reported a usable
                       IPv4 yet; re-checked with their own backoff.
``vm_state``           Fingerprint of the last successfully synced state per
                       VM, so unchanged VMs cause zero NetBox calls.
``resource_snapshot``  Last seen Proxmox resource list entry per VM, used to
                       turn list differences into events - also across restarts.
``seen_tasks``         Proxmox task UPIDs already handled (pruned after 2 days).
``kv``                 Misc. values, e.g. time of the last reconciliation.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    vmid INTEGER PRIMARY KEY,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    first_queued REAL NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS ip_wait (
    vmid INTEGER PRIMARY KEY,
    since REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_check REAL NOT NULL,
    last_reason TEXT
);
CREATE TABLE IF NOT EXISTS vm_state (
    vmid INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    netbox_id INTEGER,
    name TEXT,
    ip TEXT,
    ip_status TEXT,
    last_synced REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_snapshot (
    vmid INTEGER PRIMARY KEY,
    signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_tasks (
    upid TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class QueueItem:
    vmid: int
    reason: str
    attempts: int
    next_attempt: float
    first_queued: float
    last_error: str | None


@dataclass(frozen=True)
class IpWait:
    vmid: int
    since: float
    attempts: int
    next_check: float
    last_reason: str | None


class StateStore:
    def __init__(self, path: Path | str, *, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript(_SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO kv(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- queue
    def enqueue(self, vmid: int, reason: str, *, delay: float = 0.0, urgent: bool = True) -> None:
        """Queue a sync for one VM (one row per VMID, so duplicate events collapse).

        ``urgent`` (a real Proxmox event: something changed) pulls an entry that is
        waiting in backoff forward. Routine re-checks (``urgent=False``) never shorten
        an existing backoff, so they cannot turn into a rapid retry loop.
        """
        now = self.clock()
        self.db.execute(
            """INSERT INTO queue(vmid, reason, attempts, next_attempt, first_queued)
               VALUES (?, ?, 0, ?, ?)
               ON CONFLICT(vmid) DO UPDATE SET
                 reason = CASE WHEN instr(queue.reason, excluded.reason) > 0 THEN queue.reason
                               ELSE queue.reason || ', ' || excluded.reason END,
                 next_attempt = CASE WHEN ? THEN MIN(queue.next_attempt, excluded.next_attempt)
                                     ELSE queue.next_attempt END""",
            (vmid, reason[:200], now + delay, now, 1 if urgent else 0))
        # Keep the merged reason short.
        self.db.execute("UPDATE queue SET reason = substr(reason, 1, 300) WHERE vmid = ?", (vmid,))

    def due(self, limit: int = 50) -> list[QueueItem]:
        rows = self.db.execute("SELECT * FROM queue WHERE next_attempt <= ? ORDER BY next_attempt LIMIT ?",
                               (self.clock(), limit)).fetchall()
        return [QueueItem(**dict(r)) for r in rows]

    def queue_items(self) -> list[QueueItem]:
        return [QueueItem(**dict(r)) for r in self.db.execute("SELECT * FROM queue ORDER BY next_attempt")]

    def next_queue_time(self) -> float | None:
        row = self.db.execute("SELECT MIN(next_attempt) FROM queue").fetchone()
        return row[0] if row and row[0] is not None else None

    def complete(self, vmid: int) -> None:
        self.db.execute("DELETE FROM queue WHERE vmid = ?", (vmid,))

    def defer(self, vmid: int, delay: float, error: str) -> int:
        """Record a failed attempt; returns the new attempt count."""
        self.db.execute("UPDATE queue SET attempts = attempts + 1, next_attempt = ?, last_error = ? WHERE vmid = ?",
                        (self.clock() + delay, error[:500], vmid))
        row = self.db.execute("SELECT attempts FROM queue WHERE vmid = ?", (vmid,)).fetchone()
        return int(row[0]) if row else 0

    def postpone_all(self, until: float) -> None:
        """NetBox is down: push every due item to ``until`` without counting an attempt."""
        self.db.execute("UPDATE queue SET next_attempt = ? WHERE next_attempt < ?", (until, until))

    def retry_now(self) -> int:
        return self.db.execute("UPDATE queue SET next_attempt = ?", (self.clock(),)).rowcount

    # ------------------------------------------------------------- ip wait
    def ip_wait_get(self, vmid: int) -> IpWait | None:
        row = self.db.execute("SELECT * FROM ip_wait WHERE vmid = ?", (vmid,)).fetchone()
        return IpWait(**dict(row)) if row else None

    def ip_wait_set(self, vmid: int, *, next_check: float, reason: str) -> IpWait:
        now = self.clock()
        self.db.execute(
            """INSERT INTO ip_wait(vmid, since, attempts, next_check, last_reason) VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(vmid) DO UPDATE SET attempts = ip_wait.attempts + 1,
                 next_check = excluded.next_check, last_reason = excluded.last_reason""",
            (vmid, now, next_check, reason))
        found = self.ip_wait_get(vmid)
        assert found is not None
        return found

    def ip_wait_touch(self, vmid: int, next_check: float) -> None:
        """Move the next check without counting an attempt (used when the check was queued)."""
        self.db.execute("UPDATE ip_wait SET next_check = ? WHERE vmid = ?", (next_check, vmid))

    def ip_wait_clear(self, vmid: int) -> None:
        self.db.execute("DELETE FROM ip_wait WHERE vmid = ?", (vmid,))

    def ip_waits(self) -> list[IpWait]:
        return [IpWait(**dict(r)) for r in self.db.execute("SELECT * FROM ip_wait ORDER BY next_check")]

    def ip_waits_due(self) -> list[IpWait]:
        rows = self.db.execute("SELECT * FROM ip_wait WHERE next_check <= ?", (self.clock(),)).fetchall()
        return [IpWait(**dict(r)) for r in rows]

    def next_ip_wait_time(self) -> float | None:
        row = self.db.execute("SELECT MIN(next_check) FROM ip_wait").fetchone()
        return row[0] if row and row[0] is not None else None

    # ------------------------------------------------------------- vm state
    def fingerprint(self, vmid: int) -> str | None:
        row = self.db.execute("SELECT fingerprint FROM vm_state WHERE vmid = ?", (vmid,)).fetchone()
        return row[0] if row else None

    def record_synced(self, vmid: int, fingerprint: str, *, netbox_id: int | None, name: str,
                      ip: str | None, ip_status: str) -> None:
        self.db.execute(
            """INSERT INTO vm_state(vmid, fingerprint, netbox_id, name, ip, ip_status, last_synced)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(vmid) DO UPDATE SET fingerprint = excluded.fingerprint,
                 netbox_id = excluded.netbox_id, name = excluded.name,
                 ip = COALESCE(excluded.ip, vm_state.ip), ip_status = excluded.ip_status,
                 last_synced = excluded.last_synced""",
            (vmid, fingerprint, netbox_id, name, ip, ip_status, self.clock()))

    def forget_vm(self, vmid: int) -> None:
        for table in ("vm_state", "ip_wait", "resource_snapshot"):
            self.db.execute(f"DELETE FROM {table} WHERE vmid = ?", (vmid,))

    def vm_states(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM vm_state ORDER BY vmid")]

    # ------------------------------------------------------------- snapshots
    def snapshot(self) -> dict[int, dict[str, Any]]:
        return {int(r["vmid"]): json.loads(r["signature"])
                for r in self.db.execute("SELECT vmid, signature FROM resource_snapshot")}

    def replace_snapshot(self, snapshot: dict[int, dict[str, Any]]) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM resource_snapshot")
            self.db.executemany("INSERT INTO resource_snapshot(vmid, signature) VALUES (?, ?)",
                                [(vmid, json.dumps(sig, sort_keys=True)) for vmid, sig in snapshot.items()])

    # ------------------------------------------------------------- tasks
    def seen_task_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT upid FROM seen_tasks")}

    def mark_tasks_seen(self, upids: list[str]) -> None:
        now = self.clock()
        self.db.executemany("INSERT OR IGNORE INTO seen_tasks(upid, seen_at) VALUES (?, ?)",
                            [(u, now) for u in upids])

    def prune_tasks(self, older_than: float = 2 * 86400) -> None:
        self.db.execute("DELETE FROM seen_tasks WHERE seen_at < ?", (self.clock() - older_than,))

    # ------------------------------------------------------------- kv
    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO kv(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def get_float(self, key: str) -> float | None:
        value = self.get(key)
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None

    # ------------------------------------------------------------- helpers
    def transaction(self) -> _Tx:
        return _Tx(self.db)


class _Tx:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __enter__(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")

