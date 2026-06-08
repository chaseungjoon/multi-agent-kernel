"""LockTable: thread-safe lock state with persistence and lease expiration.

Concurrency model: every public mutation is guarded by one table-wide re-entrant
lock, so ``try_acquire_all`` is genuinely atomic — the check pass and the acquire
pass cannot be interleaved by another thread. The per-node ``RWLock`` objects are
only ever touched while this lock is held.

Lease expiry is *observable*, not silent: an expiring lease is logged
and reported to an optional ``on_expire`` callback so a scheduler can fail and
roll back the holder's task, rather than having its lock vanish underneath it.
Holders keep a lease alive by calling ``renew`` (heartbeat).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from mak.core.types import LockEntry, LockMode, NodeId, ResourceKind, ResourceRef
from mak.lock_manager.rwlock import RWLock

_logger = logging.getLogger(__name__)


class LockTable:
    """Thread-safe in-memory lock table with persistence to ``.mak/lock_table.json``."""

    def __init__(
        self,
        persist_path: Path | None = None,
        default_timeout: float = 300.0,
        on_expire: Callable[[LockEntry], None] | None = None,
    ) -> None:
        self._locks: dict[NodeId, RWLock] = {}
        self._entries: dict[NodeId, list[LockEntry]] = {}
        self._persist_path = persist_path
        self._default_timeout = default_timeout
        self._on_expire = on_expire
        self._lock = threading.RLock()

        if persist_path is not None and persist_path.exists():
            self._load_from_disk()

    def _get_rwlock(self, node_id: NodeId) -> RWLock:
        if node_id not in self._locks:
            self._locks[node_id] = RWLock()
        return self._locks[node_id]

    @staticmethod
    def _make_entry(node_id: NodeId, mode: LockMode, holder: str) -> LockEntry:
        return LockEntry(
            resource=ResourceRef(kind=ResourceKind.SYMBOL, path=str(node_id)),
            mode=mode,
            holder=holder,
            acquired_at=time.time(),
        )

    def _record(self, node_id: NodeId, entry: LockEntry) -> None:
        self._entries.setdefault(node_id, []).append(entry)

    def try_acquire(
        self,
        node_id: NodeId,
        mode: LockMode,
        holder: str,
    ) -> bool:
        """Try to acquire a lock on a single node (non-blocking)."""
        with self._lock:
            self._expire_stale()
            rwlock = self._get_rwlock(node_id)
            if not rwlock.acquire(mode, holder):
                return False
            self._record(node_id, self._make_entry(node_id, mode, holder))
            self._persist()
            return True

    def try_acquire_all(
        self,
        requests: list[tuple[NodeId, LockMode]],
        holder: str,
    ) -> bool:
        """Atomically acquire multiple locks. All-or-nothing under the table lock."""
        with self._lock:
            self._expire_stale()
            for node_id, mode in requests:
                if not self._get_rwlock(node_id).can_acquire(mode, holder):
                    return False
            for node_id, mode in requests:
                self._get_rwlock(node_id).acquire(mode, holder)
                self._record(node_id, self._make_entry(node_id, mode, holder))
            self._persist()
            return True

    def release(self, node_id: NodeId, mode: LockMode, holder: str) -> bool:
        """Release a specific lock."""
        with self._lock:
            released = self._get_rwlock(node_id).release(mode, holder)
            if released and node_id in self._entries:
                self._drop_entries(
                    node_id, lambda e: e.holder == holder and e.mode == mode, limit=1
                )
            self._persist()
            return released

    def release_all(self, holder: str) -> int:
        """Release all locks held by a specific holder."""
        with self._lock:
            count = 0
            for node_id in list(self._entries.keys()):
                for entry in list(self._entries.get(node_id, [])):
                    if entry.holder == holder:
                        self._get_rwlock(node_id).release(entry.mode, holder)
                        count += 1
                self._drop_entries(node_id, lambda e: e.holder == holder)
            self._persist()
            return count

    def renew(self, node_id: NodeId, mode: LockMode, holder: str) -> bool:
        """Heartbeat: reset a lease's clock so a live-but-slow holder is not expired."""
        with self._lock:
            entries = self._entries.get(node_id, [])
            for index, entry in enumerate(entries):
                if entry.holder == holder and entry.mode == mode:
                    entries[index] = self._make_entry(node_id, mode, holder)
                    self._persist()
                    return True
            return False

    def renew_all(self, holder: str) -> int:
        """Heartbeat every lease held by `holder`. Returns the number renewed."""
        with self._lock:
            renewed = 0
            for node_id, entries in self._entries.items():
                for index, entry in enumerate(entries):
                    if entry.holder == holder:
                        entries[index] = self._make_entry(node_id, entry.mode, holder)
                        renewed += 1
            if renewed:
                self._persist()
            return renewed

    def get_entries(self, node_id: NodeId) -> list[LockEntry]:
        """Return all lock entries for a node."""
        with self._lock:
            return list(self._entries.get(node_id, []))

    def get_holder_entries(self, holder: str) -> list[tuple[NodeId, LockEntry]]:
        """Return all lock entries held by a specific holder."""
        with self._lock:
            return [
                (node_id, entry)
                for node_id, entries in self._entries.items()
                for entry in entries
                if entry.holder == holder
            ]

    def all_entries(self) -> dict[NodeId, list[LockEntry]]:
        """Return a copy of the full lock table."""
        with self._lock:
            return {k: list(v) for k, v in self._entries.items()}

    def expire_stale(self) -> list[LockEntry]:
        """Expire timed-out leases and return them (also fires ``on_expire``)."""
        with self._lock:
            return self._expire_stale()

    def _drop_entries(
        self,
        node_id: NodeId,
        predicate: Callable[[LockEntry], bool],
        limit: int | None = None,
    ) -> None:
        """Remove entries matching `predicate` (up to `limit`), pruning empty nodes."""
        kept: list[LockEntry] = []
        removed = 0
        for entry in self._entries.get(node_id, []):
            if predicate(entry) and (limit is None or removed < limit):
                removed += 1
            else:
                kept.append(entry)
        if kept:
            self._entries[node_id] = kept
        else:
            self._entries.pop(node_id, None)

    def _expire_stale(self) -> list[LockEntry]:
        now = time.time()
        expired: list[LockEntry] = []
        for node_id in list(self._entries.keys()):
            fresh: list[LockEntry] = []
            for entry in self._entries[node_id]:
                if now - entry.acquired_at > self._default_timeout:
                    self._get_rwlock(node_id).release(entry.mode, entry.holder)
                    expired.append(entry)
                    _logger.warning(
                        "lease expired: holder=%s mode=%s node=%s (held %.1fs)",
                        entry.holder,
                        entry.mode.value,
                        node_id,
                        now - entry.acquired_at,
                    )
                else:
                    fresh.append(entry)
            if fresh:
                self._entries[node_id] = fresh
            else:
                self._entries.pop(node_id, None)
        for entry in expired:
            if self._on_expire is not None:
                self._on_expire(entry)
        return expired

    def _persist(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data: list[dict[str, object]] = [
            {
                "node_id": str(node_id),
                "mode": entry.mode.value,
                "holder": entry.holder,
                "acquired_at": entry.acquired_at,
            }
            for node_id, entries in self._entries.items()
            for entry in entries
        ]
        self._persist_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_from_disk(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _logger.warning(
                "could not load lock table from %s: %s", self._persist_path, exc
            )
            return
        for item in data:
            node_id = NodeId(str(item["node_id"]))
            mode = LockMode(str(item["mode"]))
            holder = str(item["holder"])
            self._get_rwlock(node_id).acquire(mode, holder)
            self._record(
                node_id,
                LockEntry(
                    resource=ResourceRef(kind=ResourceKind.SYMBOL, path=str(node_id)),
                    mode=mode,
                    holder=holder,
                    acquired_at=float(item["acquired_at"]),
                ),
            )
