"""Tests for mak.lock_manager.rwlock."""

from __future__ import annotations

from mak.core.types import LockMode
from mak.lock_manager.rwlock import RWLock


class TestRWLock:
    def test_read_allows_multiple_readers(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.READ, "agent_a")
        assert lock.acquire(LockMode.READ, "agent_b")
        assert "agent_a" in lock.readers
        assert "agent_b" in lock.readers

    def test_write_is_exclusive(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.WRITE, "agent_a")
        assert not lock.acquire(LockMode.WRITE, "agent_b")

    def test_write_blocks_read(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.WRITE, "agent_a")
        assert not lock.acquire(LockMode.READ, "agent_b")

    def test_read_blocks_write(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.READ, "agent_a")
        assert not lock.acquire(LockMode.WRITE, "agent_b")

    def test_same_holder_can_read_and_write(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.READ, "agent_a")
        assert lock.acquire(LockMode.WRITE, "agent_a")

    def test_intent_write_compatible_with_reads(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.READ, "agent_a")
        assert lock.acquire(LockMode.INTENT_WRITE, "agent_b")

    def test_intent_write_blocked_by_write(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.WRITE, "agent_a")
        assert not lock.acquire(LockMode.INTENT_WRITE, "agent_b")

    def test_write_blocked_by_intent_write(self) -> None:
        # Regression for risk H2: intent_write must actually exclude writers.
        lock = RWLock()
        assert lock.acquire(LockMode.INTENT_WRITE, "agent_a")
        assert not lock.acquire(LockMode.WRITE, "agent_b")

    def test_same_holder_escalates_intent_to_write(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.INTENT_WRITE, "agent_a")
        assert lock.acquire(LockMode.WRITE, "agent_a")

    def test_write_blocked_by_other_reader_not_self(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.READ, "agent_a")
        assert lock.acquire(LockMode.WRITE, "agent_a")  # self escalation ok
        lock.release(LockMode.WRITE, "agent_a")
        lock.release(LockMode.READ, "agent_a")
        assert lock.acquire(LockMode.READ, "agent_b")
        assert not lock.acquire(LockMode.WRITE, "agent_a")  # other reader blocks

    def test_multiple_intent_writers(self) -> None:
        lock = RWLock()
        assert lock.acquire(LockMode.INTENT_WRITE, "agent_a")
        assert lock.acquire(LockMode.INTENT_WRITE, "agent_b")
        assert "agent_a" in lock.intent_writers
        assert "agent_b" in lock.intent_writers

    def test_release_read(self) -> None:
        lock = RWLock()
        lock.acquire(LockMode.READ, "agent_a")
        assert lock.release(LockMode.READ, "agent_a")
        assert "agent_a" not in lock.readers

    def test_release_write(self) -> None:
        lock = RWLock()
        lock.acquire(LockMode.WRITE, "agent_a")
        assert lock.release(LockMode.WRITE, "agent_a")
        assert lock.writer is None

    def test_release_nonholder_returns_false(self) -> None:
        lock = RWLock()
        assert not lock.release(LockMode.READ, "agent_a")
        assert not lock.release(LockMode.WRITE, "agent_a")
        assert not lock.release(LockMode.INTENT_WRITE, "agent_a")

    def test_is_free(self) -> None:
        lock = RWLock()
        assert lock.is_free()
        lock.acquire(LockMode.READ, "agent_a")
        assert not lock.is_free()
        lock.release(LockMode.READ, "agent_a")
        assert lock.is_free()

    def test_holders(self) -> None:
        lock = RWLock()
        lock.acquire(LockMode.READ, "agent_a")
        lock.acquire(LockMode.INTENT_WRITE, "agent_b")
        assert lock.holders() == {"agent_a", "agent_b"}

    def test_write_after_release(self) -> None:
        lock = RWLock()
        lock.acquire(LockMode.WRITE, "agent_a")
        lock.release(LockMode.WRITE, "agent_a")
        assert lock.acquire(LockMode.WRITE, "agent_b")
