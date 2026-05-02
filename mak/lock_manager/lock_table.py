"""LockTable: in-memory lock state with persistence and timeout expiration."""

from __future__ import annotations

import json
import time
from pathlib import Path

from mak.core.exceptions import LockError
from mak.core.types import LockEntry, LockMode, NodeId, ResourceKind, ResourceRef
from mak.lock_manager.rwlock import RWLock


class LockTable:
    """In-memory lock table with persistence to .mak/lock_table.json."""

    def __init__(
        self,
        persist_path: Path | None = None,
        default_timeout: float = 300.0,
    ) -> None:
        self._locks: dict[NodeId, RWLock] = {}
        self._entries: dict[NodeId, list[LockEntry]] = {}
        self._persist_path = persist_path
        self._default_timeout = default_timeout

        if persist_path is not None and persist_path.exists():
            self._load_from_disk()

    def _get_rwlock(self, node_id: NodeId) -> RWLock:
        if node_id not in self._locks:
            self._locks[node_id] = RWLock()
        return self._locks[node_id]

    def try_acquire(
        self,
        node_id: NodeId,
        mode: LockMode,
        holder: str,
        timeout: float | None = None,
    ) -> bool:
        """Try to acquire a lock on a single node."""
        self._expire_stale()
        rwlock = self._get_rwlock(node_id)

        if not rwlock.can_acquire(mode, holder):
            return False

        rwlock.acquire(mode, holder)

        entry = LockEntry(
            resource=ResourceRef(kind=ResourceKind.SYMBOL, path=str(node_id)),
            mode=mode,
            holder=holder,
            acquired_at=time.time(),
        )
        if node_id not in self._entries:
            self._entries[node_id] = []
        self._entries[node_id].append(entry)

        self._persist()
        return True

    def try_acquire_all(
        self,
        requests: list[tuple[NodeId, LockMode]],
        holder: str,
    ) -> bool:
        """Atomically acquire multiple locks. All-or-nothing."""
        self._expire_stale()

        for node_id, mode in requests:
            rwlock = self._get_rwlock(node_id)
            if not rwlock.can_acquire(mode, holder):
                return False

        for node_id, mode in requests:
            rwlock = self._get_rwlock(node_id)
            rwlock.acquire(mode, holder)
            entry = LockEntry(
                resource=ResourceRef(kind=ResourceKind.SYMBOL, path=str(node_id)),
                mode=mode,
                holder=holder,
                acquired_at=time.time(),
            )
            if node_id not in self._entries:
                self._entries[node_id] = []
            self._entries[node_id].append(entry)

        self._persist()
        return True

    def release(self, node_id: NodeId, mode: LockMode, holder: str) -> bool:
        """Release a specific lock."""
        rwlock = self._get_rwlock(node_id)
        released = rwlock.release(mode, holder)

        if released and node_id in self._entries:
            self._entries[node_id] = [
                e
                for e in self._entries[node_id]
                if not (e.holder == holder and e.mode == mode)
            ]
            if not self._entries[node_id]:
                del self._entries[node_id]

        self._persist()
        return released

    def release_all(self, holder: str) -> int:
        """Release all locks held by a specific holder."""
        count = 0
        for node_id in list(self._entries.keys()):
            entries = self._entries.get(node_id, [])
            for entry in entries:
                if entry.holder == holder:
                    rwlock = self._get_rwlock(node_id)
                    rwlock.release(entry.mode, holder)
                    count += 1
            self._entries[node_id] = [
                e for e in entries if e.holder != holder
            ]
            if not self._entries[node_id]:
                del self._entries[node_id]

        self._persist()
        return count

    def get_entries(self, node_id: NodeId) -> list[LockEntry]:
        """Return all lock entries for a node."""
        return list(self._entries.get(node_id, []))

    def get_holder_entries(self, holder: str) -> list[tuple[NodeId, LockEntry]]:
        """Return all lock entries held by a specific holder."""
        result: list[tuple[NodeId, LockEntry]] = []
        for node_id, entries in self._entries.items():
            for entry in entries:
                if entry.holder == holder:
                    result.append((node_id, entry))
        return result

    def all_entries(self) -> dict[NodeId, list[LockEntry]]:
        """Return a copy of the full lock table."""
        return {k: list(v) for k, v in self._entries.items()}

    def _expire_stale(self) -> None:
        now = time.time()
        for node_id in list(self._entries.keys()):
            entries = self._entries[node_id]
            fresh: list[LockEntry] = []
            for entry in entries:
                if now - entry.acquired_at > self._default_timeout:
                    rwlock = self._get_rwlock(node_id)
                    rwlock.release(entry.mode, entry.holder)
                else:
                    fresh.append(entry)
            if fresh:
                self._entries[node_id] = fresh
            else:
                self._entries.pop(node_id, None)

    def _persist(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data: list[dict[str, object]] = []
        for node_id, entries in self._entries.items():
            for entry in entries:
                data.append(
                    {
                        "node_id": str(node_id),
                        "mode": entry.mode.value,
                        "holder": entry.holder,
                        "acquired_at": entry.acquired_at,
                    }
                )
        self._persist_path.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def _load_from_disk(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in data:
            node_id = NodeId(str(item["node_id"]))
            mode = LockMode(str(item["mode"]))
            holder = str(item["holder"])
            acquired_at = float(item["acquired_at"])

            rwlock = self._get_rwlock(node_id)
            rwlock.acquire(mode, holder)

            entry = LockEntry(
                resource=ResourceRef(kind=ResourceKind.SYMBOL, path=str(node_id)),
                mode=mode,
                holder=holder,
                acquired_at=acquired_at,
            )
            if node_id not in self._entries:
                self._entries[node_id] = []
            self._entries[node_id].append(entry)
