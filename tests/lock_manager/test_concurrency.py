"""Concurrency stress tests for the lock table.

Drives many threads at a shared set of nodes and asserts the core safety
invariant never breaks: a node is never simultaneously write-held by two holders,
and a writer never coexists with a reader.
"""

from __future__ import annotations

import threading

from mak.core.types import LockMode, NodeId
from mak.lock_manager.lock_table import LockTable


def _hammer(
    table: LockTable,
    node_id: NodeId,
    mode: LockMode,
    holder: str,
    iterations: int,
    violations: list[str],
    live: dict[str, set[str]],
    live_guard: threading.Lock,
) -> None:
    for _ in range(iterations):
        if not table.try_acquire(node_id, mode, holder):
            continue
        with live_guard:
            live.setdefault("writers", set())
            live.setdefault("readers", set())
            if mode is LockMode.WRITE:
                live["writers"].add(holder)
                if len(live["writers"]) > 1:
                    violations.append(f"two writers: {live['writers']}")
                if live["readers"]:
                    violations.append("writer with readers")
            else:
                live["readers"].add(holder)
                if live["writers"]:
                    violations.append("reader with writer")
        with live_guard:
            live.get("writers", set()).discard(holder)
            live.get("readers", set()).discard(holder)
        table.release(node_id, mode, holder)


def test_no_conflicting_holders_under_contention() -> None:
    table = LockTable()
    node_id = NodeId("hot.py::function::f")
    violations: list[str] = []
    live: dict[str, set[str]] = {}
    live_guard = threading.Lock()

    threads: list[threading.Thread] = []
    for i in range(12):
        mode = LockMode.WRITE if i % 2 == 0 else LockMode.READ
        thread = threading.Thread(
            target=_hammer,
            args=(
                table, node_id, mode, f"agent_{i}", 400,
                violations, live, live_guard,
            ),
        )
        threads.append(thread)

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert violations == [], violations[:5]


def test_try_acquire_all_is_atomic_under_contention() -> None:
    # Two holders repeatedly contend for the same two nodes as one atomic unit.
    # The all-or-nothing guarantee must hold: a holder never ends up with a
    # partial grant that lets a peer also grab one of the nodes for writing.
    table = LockTable()
    n1 = NodeId("a.py::function::a")
    n2 = NodeId("b.py::function::b")
    partial_grants: list[str] = []

    requests = [(n1, LockMode.WRITE), (n2, LockMode.WRITE)]

    def worker(holder: str) -> None:
        for _ in range(500):
            if table.try_acquire_all(requests, holder):
                e1 = {x.holder for x in table.get_entries(n1)}
                e2 = {x.holder for x in table.get_entries(n2)}
                if e1 != {holder} or e2 != {holder}:
                    partial_grants.append(holder)
                table.release_all(holder)

    threads = [threading.Thread(target=worker, args=(f"h{i}",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert partial_grants == []
    assert table.all_entries() == {}
