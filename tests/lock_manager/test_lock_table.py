"""Tests for mak.lock_manager.lock_table."""

from __future__ import annotations

import time
from pathlib import Path

from mak.core.types import LockMode, NodeId
from mak.lock_manager.lock_table import LockTable


class TestLockTable:
    def test_acquire_and_release(self) -> None:
        table = LockTable()
        nid = NodeId("mod.py::function::foo")
        assert table.try_acquire(nid, LockMode.WRITE, "agent_a")
        assert table.release(nid, LockMode.WRITE, "agent_a")

    def test_concurrent_reads(self) -> None:
        table = LockTable()
        nid = NodeId("mod.py::function::foo")
        assert table.try_acquire(nid, LockMode.READ, "agent_a")
        assert table.try_acquire(nid, LockMode.READ, "agent_b")

    def test_write_blocks_write(self) -> None:
        table = LockTable()
        nid = NodeId("mod.py::function::foo")
        assert table.try_acquire(nid, LockMode.WRITE, "agent_a")
        assert not table.try_acquire(nid, LockMode.WRITE, "agent_b")

    def test_try_acquire_all_atomic(self) -> None:
        table = LockTable()
        n1 = NodeId("a.py::function::a")
        n2 = NodeId("b.py::function::b")
        requests = [(n1, LockMode.WRITE), (n2, LockMode.WRITE)]
        assert table.try_acquire_all(requests, "agent_a")
        entries_a = table.get_holder_entries("agent_a")
        assert len(entries_a) == 2

    def test_try_acquire_all_fails_atomically(self) -> None:
        table = LockTable()
        n1 = NodeId("a.py::function::a")
        n2 = NodeId("b.py::function::b")
        table.try_acquire(n2, LockMode.WRITE, "agent_x")
        requests = [(n1, LockMode.WRITE), (n2, LockMode.WRITE)]
        assert not table.try_acquire_all(requests, "agent_a")
        assert table.get_entries(n1) == []

    def test_release_all(self) -> None:
        table = LockTable()
        n1 = NodeId("a.py::function::a")
        n2 = NodeId("b.py::function::b")
        table.try_acquire_all(
            [(n1, LockMode.WRITE), (n2, LockMode.READ)], "agent_a"
        )
        count = table.release_all("agent_a")
        assert count == 2
        assert table.get_holder_entries("agent_a") == []

    def test_get_entries(self) -> None:
        table = LockTable()
        nid = NodeId("mod.py::function::foo")
        table.try_acquire(nid, LockMode.READ, "agent_a")
        entries = table.get_entries(nid)
        assert len(entries) == 1
        assert entries[0].holder == "agent_a"

    def test_all_entries(self) -> None:
        table = LockTable()
        n1 = NodeId("a.py::function::a")
        n2 = NodeId("b.py::function::b")
        table.try_acquire(n1, LockMode.WRITE, "agent_a")
        table.try_acquire(n2, LockMode.READ, "agent_b")
        all_e = table.all_entries()
        assert n1 in all_e
        assert n2 in all_e

    def test_persistence_round_trip(self, tmp_path: Path) -> None:
        persist = tmp_path / "locks.json"
        table1 = LockTable(persist_path=persist)
        nid = NodeId("mod.py::function::foo")
        table1.try_acquire(nid, LockMode.WRITE, "agent_a")
        assert persist.exists()

        table2 = LockTable(persist_path=persist)
        entries = table2.get_entries(nid)
        assert len(entries) == 1
        assert entries[0].holder == "agent_a"

    def test_timeout_expiration(self) -> None:
        table = LockTable(default_timeout=0.01)
        nid = NodeId("mod.py::function::foo")
        table.try_acquire(nid, LockMode.WRITE, "agent_a")
        time.sleep(0.02)
        assert table.try_acquire(nid, LockMode.WRITE, "agent_b")

    def test_release_nonexistent_returns_false(self) -> None:
        table = LockTable()
        nid = NodeId("mod.py::function::foo")
        assert not table.release(nid, LockMode.WRITE, "agent_a")

    def test_expiry_is_observable(self) -> None:
        # Risk H3: an expiring lease must be reported, not silently stolen.
        expired: list[str] = []
        table = LockTable(
            default_timeout=0.01, on_expire=lambda e: expired.append(e.holder)
        )
        nid = NodeId("mod.py::function::foo")
        table.try_acquire(nid, LockMode.WRITE, "agent_a")
        time.sleep(0.02)
        reported = table.expire_stale()
        assert [e.holder for e in reported] == ["agent_a"]
        assert expired == ["agent_a"]

    def test_renew_keeps_lease_alive(self) -> None:
        # A heartbeat resets the clock so a live-but-slow holder is not expired.
        table = LockTable(default_timeout=0.05)
        nid = NodeId("mod.py::function::foo")
        table.try_acquire(nid, LockMode.WRITE, "agent_a")
        for _ in range(4):
            time.sleep(0.03)
            assert table.renew(nid, LockMode.WRITE, "agent_a")
        # Still held by agent_a; a peer cannot take it.
        assert not table.try_acquire(nid, LockMode.WRITE, "agent_b")

    def test_renew_unknown_lease_returns_false(self) -> None:
        table = LockTable()
        nid = NodeId("mod.py::function::foo")
        assert not table.renew(nid, LockMode.WRITE, "ghost")
