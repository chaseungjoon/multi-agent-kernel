"""Per-node reader-writer lock primitive."""

from __future__ import annotations

from mak.core.types import LockMode


class RWLock:
    """Reader-writer lock supporting read, write, and intent_write modes."""

    def __init__(self) -> None:
        self._readers: set[str] = set()
        self._writer: str | None = None
        self._intent_writers: set[str] = set()

    @property
    def readers(self) -> frozenset[str]:
        return frozenset(self._readers)

    @property
    def writer(self) -> str | None:
        return self._writer

    @property
    def intent_writers(self) -> frozenset[str]:
        return frozenset(self._intent_writers)

    def is_free(self) -> bool:
        return not self._readers and self._writer is None and not self._intent_writers

    def can_acquire(self, mode: LockMode, holder: str) -> bool:
        """Check if the given mode can be acquired without blocking."""
        if mode == LockMode.READ:
            return self._writer is None or self._writer == holder

        if mode == LockMode.WRITE:
            if self._writer is not None and self._writer != holder:
                return False
            other_readers = self._readers - {holder}
            return len(other_readers) == 0

        if mode == LockMode.INTENT_WRITE:
            return self._writer is None or self._writer == holder

        return False  # pragma: no cover

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
        if mode == LockMode.READ:
            if holder in self._readers:
                self._readers.discard(holder)
                return True
            return False

        if mode == LockMode.WRITE:
            if self._writer == holder:
                self._writer = None
                return True
            return False

        if mode == LockMode.INTENT_WRITE:
            if holder in self._intent_writers:
                self._intent_writers.discard(holder)
                return True
            return False

        return False  # pragma: no cover

    def holders(self) -> set[str]:
        """Return all current holders."""
        result: set[str] = set()
        result.update(self._readers)
        if self._writer is not None:
            result.add(self._writer)
        result.update(self._intent_writers)
        return result
