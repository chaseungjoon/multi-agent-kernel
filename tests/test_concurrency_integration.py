"""Wave 5 thesis gate: real concurrent sessions over an overlapping corpus.

This is the integration test that licenses MAK's "functionally complete" claim.
Unlike ``tests/lock_manager/test_concurrency.py`` (which drives the lock table in
isolation), this drives the *whole pipeline* — scheduler dispatch onto a thread
pool, agents staging into the shared node store concurrently, batched conflict
detection, transactional commit, and file reconstruction — over a multi-file
corpus whose tasks **deliberately overlap** on the same target nodes.

It asserts the four shared-memory invariants:

1. No two conflicting holders ever coexist (a node is write-held by at most one
   live agent at a time).
2. No lost or corrupted fragment (every targeted node ends edited, every file is
   valid Python).
3. The node store stays consistent with disk (a fresh reconstruction equals the
   bytes on disk).
4. Deadlocks are detected and resolved: atomic lock pre-allocation makes the live
   pipeline deadlock-free, so the heavily-contended run terminates and every task
   completes — no permanent stall, no blocked tasks.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

from mak.config import GitConfig, MakConfig, NodeStoreConfig, SessionConfig
from mak.core.types import (
    LockMode,
    NodeFragment,
    NodeId,
    SubTask,
    TaskBundle,
    TaskResult,
)
from mak.lock_manager.lock_table import LockTable
from mak.node_store.reconstruction import assemble_fragments
from mak.node_store.store import NodeStore
from mak.session import Session

# Three files, three functions each → nine independently lockable nodes.
_FILES = ("alpha.py", "beta.py", "gamma.py")
_FUNCS = ("f0", "f1", "f2")


def _file_source() -> str:
    bodies = [f"def {name}():\n    return {i}\n" for i, name in enumerate(_FUNCS)]
    return "\n\n".join(bodies) + "\n"


def _func_name(node_id: NodeId) -> str:
    return str(node_id).split("::")[2]


class _ContendedRunner:
    """A real agent that rewrites its target nodes, asserting lock exclusivity.

    While it runs it holds the write lock on each target node; it checks the live
    lock table to confirm *it* is the sole writer, and registers itself in a
    shared ``live`` map so the test catches any two agents writing one node at
    once. A small sleep widens the concurrency window so overlap actually races.
    """

    def __init__(
        self,
        store: NodeStore,
        lock_table: LockTable,
        violations: list[str],
        live: dict[str, set[str]],
        guard: threading.Lock,
    ) -> None:
        self._store = store
        self._lock_table = lock_table
        self._violations = violations
        self._live = live
        self._guard = guard

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        with self._guard:
            for node_id in task.target_nodes:
                writers = {
                    entry.holder
                    for entry in self._lock_table.get_entries(node_id)
                    if entry.mode is LockMode.WRITE
                }
                if writers != {task.task_id}:
                    self._violations.append(
                        f"{node_id}: write holders {writers} != {{{task.task_id}}}"
                    )
                live_now = self._live.setdefault(str(node_id), set())
                live_now.add(task.task_id)
                if len(live_now) > 1:
                    self._violations.append(
                        f"{node_id}: concurrent writers {live_now}"
                    )
        # Widen the window: hold the "edit" open so genuinely concurrent tasks
        # on distinct nodes overlap, and same-node tasks must serialize.
        time_to_yield()
        done: list[NodeId] = []
        for node_id in task.target_nodes:
            name = _func_name(node_id)
            new_source = f"def {name}():\n    return {task.task_id!r}\n"
            self._store.put_node(
                node_id, NodeFragment(node_id, "function", new_source, 1)
            )
            done.append(node_id)
        with self._guard:
            for node_id in task.target_nodes:
                self._live[str(node_id)].discard(task.task_id)
        return TaskResult(
            task_id=task.task_id, success=True, modified_nodes=list(done)
        )


def time_to_yield() -> None:
    """Brief sleep to provoke real thread interleaving without slowing the suite."""
    import time

    time.sleep(0.01)


class _Registry:
    def get(self, agent_type: str) -> object:
        return object()


def _config(tmp_path: Path) -> MakConfig:
    return MakConfig(
        session=SessionConfig(
            work_dir=str(tmp_path),
            mak_dir=str(tmp_path / ".mak"),
            max_concurrent_agents=4,
            deadlock_check_interval_s=0.0,  # scan the wait graph every iteration
        ),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(),
    )


def _overlapping_plan() -> list[SubTask]:
    """Two tasks per node: same-node tasks contend, others run in parallel."""
    plan: list[SubTask] = []
    for file in _FILES:
        for func in _FUNCS:
            node = f"{file}::function::{func}"
            for round_ in (0, 1):
                plan.append(
                    SubTask(
                        task_id=f"t-{file[0]}-{func}-{round_}",
                        description=f"rewrite {node} (round {round_})",
                        target_nodes=[NodeId(node)],
                        agent_type="fake",
                    )
                )
    return plan


def test_concurrent_overlapping_sessions_hold_all_invariants(tmp_path: Path) -> None:
    for file in _FILES:
        (tmp_path / file).write_text(_file_source())

    store = NodeStore(tmp_path / ".mak" / "node_store")
    lock_table = LockTable()
    violations: list[str] = []
    live: dict[str, set[str]] = {}
    guard = threading.Lock()
    runner = _ContendedRunner(store, lock_table, violations, live, guard)

    session = Session(
        session_id="concurrency-gate",
        config=_config(tmp_path),
        node_store=store,
        lock_table=lock_table,
        registry=_Registry(),  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )
    session.initialize()
    plan = _overlapping_plan()
    session.install_plan(plan)
    result = session.run()

    # (4) Deadlock-free under contention: the run terminated with everything done.
    assert result.ok, (result.state, result.failed, result.blocked)
    assert set(result.completed) == {t.task_id for t in plan}
    assert result.failed == ()
    assert result.blocked == ()

    # (1) No two conflicting holders ever coexisted.
    assert violations == [], violations[:8]

    # All leases were released once the run finished.
    assert lock_table.all_entries() == {}

    for file in _FILES:
        # (2) No lost/corrupted fragment: every node was edited and the file parses.
        for node_id in store.list_nodes(file):
            fragment = store.get_node(node_id)
            assert fragment.version >= 2, f"{node_id} never advanced past ingest"

        # (3) Store consistent with disk: a reconstruction from the committed
        # fragments is semantically identical to the bytes on disk (disk is
        # ruff-formatted, so compare ASTs, not raw text), and both are valid.
        on_disk = (tmp_path / file).read_text()
        compile(on_disk, file, "exec")
        rebuilt = assemble_fragments(store.get_committed_fragments(file))
        assert ast.dump(ast.parse(rebuilt)) == ast.dump(ast.parse(on_disk))


def test_overlap_serializes_same_node_writes(tmp_path: Path) -> None:
    """Two tasks on one node never run together; the later commit wins on disk."""
    (tmp_path / "solo.py").write_text("def only():\n    return 0\n")
    store = NodeStore(tmp_path / ".mak" / "node_store")
    lock_table = LockTable()
    violations: list[str] = []
    live: dict[str, set[str]] = {}
    runner = _ContendedRunner(store, lock_table, violations, live, threading.Lock())

    session = Session(
        session_id="overlap",
        config=_config(tmp_path),
        node_store=store,
        lock_table=lock_table,
        registry=_Registry(),  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
    )
    session.initialize()
    node = "solo.py::function::only"
    session.install_plan(
        [
            SubTask(task_id="w1", description="d", target_nodes=[NodeId(node)],
                    agent_type="fake"),
            SubTask(task_id="w2", description="d", target_nodes=[NodeId(node)],
                    agent_type="fake"),
        ]
    )
    result = session.run()

    assert result.ok
    assert set(result.completed) == {"w1", "w2"}
    assert violations == []
    # Ingest (v1) + two serialized commits (v2, v3).
    assert store.get_node(NodeId(node)).version == 3
