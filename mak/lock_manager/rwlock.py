"""Per-node reader-writer lock primitive.

Grant decisions are driven by the canonical conflict matrix in
``mak.lock_manager.conflicts`` so that the lock and the deadlock detector share
one definition of "conflict". ``RWLock`` is not internally synchronized — it is
designed to be mutated only while the owning ``LockTable`` holds its table-wide
lock (see ``LockTable`` for the concurrency model).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import assert_never

from mak.core.types import LockMode
from mak.lock_manager.conflicts import conflicts


class RWLock:
    """Reader-writer lock supporting read, write, and intent_write modes."""

    def __init__(self) -> None:
        self._readers: set[str] = set()
        self._writer: str | None = None
        self._intent_writers: set[str] = set()

    @property
    def readers(self) -> frozenset[str]:
        """Holders currently holding a read lock."""
        return frozenset(self._readers)

    @property
    def writer(self) -> str | None:
        """The exclusive write-lock holder, if any."""
        return self._writer

    @property
    def intent_writers(self) -> frozenset[str]:
        """Holders that have declared intent to write."""
        return frozenset(self._intent_writers)

    def is_free(self) -> bool:
        """Return True if no holder has any lock on this node."""
        return not self._readers and self._writer is None and not self._intent_writers

    def _held(self) -> Iterator[tuple[str, LockMode]]:
        """Yield every (holder, mode) pair currently active on this lock."""
        for reader in self._readers:
            yield reader, LockMode.READ
        if self._writer is not None:
            yield self._writer, LockMode.WRITE
        for intent in self._intent_writers:
            yield intent, LockMode.INTENT_WRITE

    def can_acquire(self, mode: LockMode, holder: str) -> bool:
        """Check if the given mode can be acquired without blocking.

        A holder never conflicts with itself, so the same agent may escalate
        (e.g. read -> write) on a node it already holds.
        """
        return not any(
            other != holder and conflicts(mode, held_mode)
            for other, held_mode in self._held()
        )

    def acquire(self, mode: LockMode, holder: str) -> bool:
        """Try to acquire the lock. Returns True on success."""
        if not self.can_acquire(mode, holder):
            return False

        if mode == LockMode.READ:
            self._readers.add(holder)
        elif mode == LockMode.WRITE:
            self._writer = holder
        elif mode == LockMode.INTENT_WRITE:
            self._intent_writers.add(holder)

        return True

    def release(self, mode: LockMode, holder: str) -> bool:
        """Release the lock. Returns True if holder actually held it."""
        if mode is LockMode.READ:
            held = holder in self._readers
            self._readers.discard(holder)
            return held

        if mode is LockMode.WRITE:
            if self._writer == holder:
                self._writer = None
                return True
            return False

        if mode is LockMode.INTENT_WRITE:
            held = holder in self._intent_writers
            self._intent_writers.discard(holder)
            return held

        assert_never(mode)

    def holders(self) -> set[str]:
        """Return all current holders."""
        return {holder for holder, _ in self._held()}
