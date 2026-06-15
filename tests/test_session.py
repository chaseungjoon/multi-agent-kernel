"""Tests for mak.session: lifecycle, dispatch, partial completion, recovery."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from mak.config import GitConfig, MakConfig, NodeStoreConfig, SessionConfig
from mak.core.exceptions import SessionError
from mak.core.types import (
    LockMode,
    NodeFragment,
    NodeId,
    SubTask,
    TaskBundle,
    TaskResult,
)
from mak.git_integration.git import GitHelper
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.session import Session, SessionState, SubTaskProgress, _Completion

# --- fakes -------------------------------------------------------------------


class FakeAdapter:
    agent_type = "fake"


class FakeRegistry:
    def get(self, agent_type: str) -> FakeAdapter:
        return FakeAdapter()


class StagingRunner:
    """An agent that stages new fragment versions for some/all target nodes.

    ``coverage`` maps a task_id to the fraction of target nodes it completes on
    each attempt (1.0 = full, 0.5 = half, 0.0 = none/fail). A list provides a
    per-attempt schedule; a scalar applies to every attempt.
    """

    def __init__(
        self,
        node_store: NodeStore,
        coverage: dict[str, object] | None = None,
        *,
        new_source: str = "x = 1\n",
    ) -> None:
        self._node_store = node_store
        self._coverage = coverage or {}
        self._new_source = new_source
        self._attempts: dict[str, int] = {}
        self.assigned: list[TaskBundle] = []

    def _fraction(self, task_id: str) -> float:
        spec = self._coverage.get(task_id, 1.0)
        attempt = self._attempts.get(task_id, 0)
        if isinstance(spec, list):
            return float(spec[min(attempt, len(spec) - 1)])
        return float(spec)  # type: ignore[arg-type]

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        self.assigned.append(task)
        fraction = self._fraction(task.task_id)
        self._attempts[task.task_id] = self._attempts.get(task.task_id, 0) + 1
        count = int(round(len(task.target_nodes) * fraction))
        done = task.target_nodes[:count]
        for node_id in done:
            self._node_store.put_node(
                node_id,
                NodeFragment(node_id, "function", self._new_source, 1),
            )
        return TaskResult(
            task_id=task.task_id,
            success=bool(done),
            modified_nodes=list(done),
        )


def _config(tmp_path: Path) -> MakConfig:
    return MakConfig(
        session=SessionConfig(work_dir=str(tmp_path), mak_dir=str(tmp_path / ".mak")),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(),
    )


def _store(tmp_path: Path) -> NodeStore:
    return NodeStore(tmp_path / "store")


# A two-function module used by tests that need two writable nodes in one file.
_TWO_FUNCS = "def a():\n    return 0\n\n\ndef b():\n    return 0\n"


def _session(
    tmp_path: Path,
    *,
    runner: object,
    node_store: NodeStore,
    lock_table: LockTable | None = None,
    test_runner: object = None,
    max_attempts: int = 3,
    git_helper: object = None,
) -> Session:
    return Session(
        session_id="s1",
        config=_config(tmp_path),
        node_store=node_store,
        lock_table=lock_table or LockTable(),
        registry=FakeRegistry(),  # type: ignore[arg-type]
        agent_runner=runner,
        git_helper=git_helper,  # type: ignore[arg-type]
        test_runner=test_runner,  # type: ignore[arg-type]
        max_attempts=max_attempts,
    )


def _task(
    task_id: str,
    nodes: list[str],
    deps: list[str] | None = None,
    context: list[str] | None = None,
) -> SubTask:
    return SubTask(
        task_id=task_id,
        description=f"task {task_id}",
        target_nodes=[NodeId(n) for n in nodes],
        context_nodes=[NodeId(n) for n in (context or [])],
        depends_on=deps or [],
        agent_type="fake",
    )


# --- progress dataclass ------------------------------------------------------


class TestSubTaskProgress:
    def test_remaining_and_complete(self) -> None:
        p = SubTaskProgress("t", [NodeId("a"), NodeId("b")])
        assert p.remaining == [NodeId("a"), NodeId("b")]
        assert not p.is_complete
        p.completed_nodes.add(NodeId("a"))
        assert p.remaining == [NodeId("b")]
        p.completed_nodes.add(NodeId("b"))
        assert p.is_complete


# --- initialize --------------------------------------------------------------


class TestInitialize:
    def test_ingests_python_files(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text("def f():\n    return 1\n")
        store = _store(tmp_path)
        session = _session(tmp_path, runner=StagingRunner(store), node_store=store)
        inventory = session.initialize()
        assert any("mod.py" in str(n) for n in inventory)
        assert session.state is SessionState.INITIALIZED

    def test_skips_syntactically_invalid_files(self, tmp_path: Path) -> None:
        (tmp_path / "good.py").write_text("x = 1\n")
        (tmp_path / "bad.py").write_text("def (:\n")
        store = _store(tmp_path)
        session = _session(tmp_path, runner=StagingRunner(store), node_store=store)
        inventory = session.initialize()
        assert any("good.py" in str(n) for n in inventory)
        assert not any("bad.py" in str(n) for n in inventory)

    def test_double_initialize_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        session = _session(tmp_path, runner=StagingRunner(store), node_store=store)
        session.initialize()
        with pytest.raises(SessionError, match="cannot initialize"):
            session.initialize()


# --- plan / install ----------------------------------------------------------


class TestInstallPlan:
    def test_run_requires_plan(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        session = _session(tmp_path, runner=StagingRunner(store), node_store=store)
        session.initialize()
        with pytest.raises(SessionError, match="cannot run"):
            session.run()

    def test_install_requires_initialized(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        session = _session(tmp_path, runner=StagingRunner(store), node_store=store)
        with pytest.raises(SessionError, match="cannot install"):
            session.install_plan([_task("a", ["m.py::function::a"])])

    def test_plan_without_planner_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        session = _session(tmp_path, runner=StagingRunner(store), node_store=store)
        session.initialize()
        with pytest.raises(SessionError, match="no planner"):
            session.plan("do stuff", review=False)


# --- run: full completion ----------------------------------------------------


class TestRunFullCompletion:
    def test_single_task_completes(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        runner = StagingRunner(store)
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        assert result.ok
        assert result.completed == ("a",)
        assert session.state is SessionState.COMPLETED
        # The new fragment was committed and the file rewritten.
        assert "x = 1" in (tmp_path / "m.py").read_text()

    def test_dependency_chain_runs_in_order(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)
        runner = StagingRunner(store)
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan(
            [
                _task("a", ["m.py::function::a"]),
                _task("b", ["m.py::function::b"], deps=["a"]),
            ]
        )
        result = session.run()
        assert result.ok
        assert [b.task_id for b in runner.assigned] == ["a", "b"]

    def test_locks_released_after_completion(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        lock_table = LockTable()
        session = _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            lock_table=lock_table,
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        session.run()
        assert lock_table.all_entries() == {}


# --- run: partial completion -------------------------------------------------


class TestPartialCompletion:
    def test_partial_then_finish(self, tmp_path: Path) -> None:
        # Task 'a' writes two nodes; first attempt completes half, second the rest.
        src = "def a():\n    return 0\n\n\ndef a2():\n    return 0\n"
        (tmp_path / "m.py").write_text(src)
        store = _store(tmp_path)
        runner = StagingRunner(store, coverage={"a": [0.5, 1.0]})
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan(
            [_task("a", ["m.py::function::a", "m.py::function::a2"])]
        )
        result = session.run()
        assert result.ok
        # First attempt covered 1 node, narrowed re-dispatch covered the other.
        assert len(runner.assigned) == 2
        assert len(runner.assigned[1].target_nodes) == 1

    def test_partial_progress_preserved_across_attempts(self, tmp_path: Path) -> None:
        src = "def a():\n    return 0\n\n\ndef a2():\n    return 0\n"
        (tmp_path / "m.py").write_text(src)
        store = _store(tmp_path)
        runner = StagingRunner(store, coverage={"a": [0.5, 1.0]})
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan(
            [_task("a", ["m.py::function::a", "m.py::function::a2"])]
        )
        session.run()
        # The second dispatch only re-targets the node left over from the first.
        assert runner.assigned[1].target_nodes == [NodeId("m.py::function::a2")]

    def test_agent_error_is_surfaced_on_result(self, tmp_path: Path) -> None:
        # An agent whose call fails (e.g. API error / truncated response) reports an
        # error; the run must carry that reason so the failure is diagnosable.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)

        class ErroringRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error="response truncated (hit max_tokens)",
                )

        session = _session(
            tmp_path, runner=ErroringRunner(), node_store=store, max_attempts=2
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        assert result.failed == ("a",)
        assert "truncated" in result.failure_reasons["a"]

    def test_never_completes_fails_after_max_attempts(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        runner = StagingRunner(store, coverage={"a": 0.0})  # always fails
        session = _session(
            tmp_path, runner=runner, node_store=store, max_attempts=3
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        assert not result.ok
        assert result.failed == ("a",)
        assert session.state is SessionState.FAILED
        assert len(runner.assigned) == 3  # bounded by max_attempts

    def test_failed_task_releases_locks(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        lock_table = LockTable()
        runner = StagingRunner(store, coverage={"a": 0.0})
        session = _session(
            tmp_path,
            runner=runner,
            node_store=store,
            lock_table=lock_table,
            max_attempts=2,
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        session.run()
        assert lock_table.all_entries() == {}


# --- teardown ----------------------------------------------------------------


class TestTeardown:
    def test_teardown_runs_tests(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        calls: list[str] = []

        def test_runner() -> tuple[bool, str]:
            calls.append("ran")
            return True, "ok"

        session = _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            test_runner=test_runner,
        )
        assert session.teardown() is True
        assert calls == ["ran"]

    def test_teardown_reports_failing_tests(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        session = _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            test_runner=lambda: (False, "boom"),
        )
        assert session.teardown() is False


# --- crash recovery ----------------------------------------------------------


class TestRecovery:
    def test_recover_requeues_incomplete_tasks(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)
        lock_table = LockTable(persist_path=tmp_path / ".mak" / "lock_table.json")

        # First session: complete 'a', then simulate a crash before 'b'.
        runner1 = StagingRunner(store)
        s1 = _session(
            tmp_path, runner=runner1, node_store=store, lock_table=lock_table
        )
        s1.initialize()
        s1.install_plan(
            [
                _task("a", ["m.py::function::a"]),
                _task("b", ["m.py::function::b"], deps=["a"]),
            ]
        )
        # Drive only the first task to completion, then stop (crash). Only 'a' is
        # ready ('b' depends on it), so the batch is exactly {a}.
        s1._scheduler.tick()  # dispatch a onto the pool
        s1._process_batch(s1._collect_batch())
        s1.close()
        assert s1._completed == ["a"]

        # Second session recovers from the persisted task graph.
        store2 = NodeStore(tmp_path / "store")
        runner2 = StagingRunner(store2)
        s2 = _session(
            tmp_path,
            runner=runner2,
            node_store=store2,
            lock_table=LockTable(persist_path=tmp_path / ".mak" / "lock_table.json"),
        )
        s2.recover()
        assert s2.state is SessionState.PLANNED
        result = s2.run()
        # 'b' is the only remaining task; recovery re-queued it.
        assert "b" in result.completed

    def test_recover_expires_stale_locks(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".mak" / "lock_table.json"
        # A lock table with a 0s timeout: any held lease is immediately stale.
        lt = LockTable(persist_path=lock_path, default_timeout=0.0)
        lt.try_acquire(NodeId("m.py::function::a"), LockMode.WRITE, "ghost")
        store = _store(tmp_path)
        session = _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            lock_table=LockTable(persist_path=lock_path, default_timeout=0.0),
        )
        expired = session.recover()
        assert expired >= 1


# --- session hardening -------------------------------------------------------


class TestBundleEnrichment:
    """Agents must receive write + read source, not just node ids."""

    def test_bundle_carries_write_and_read_source(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)
        runner = StagingRunner(store)
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan(
            [_task("a", ["m.py::function::a"], context=["m.py::function::b"])]
        )
        session.run()
        bundle = runner.assigned[0]
        # The agent sees the source it will edit and the read-only context node.
        assert "write_source:m.py::function::a" in bundle.context
        assert "read_source:m.py::function::b" in bundle.context
        # Write source is the original committed code (before this task's edit).
        assert "return 0" in bundle.context["write_source:m.py::function::a"]

    def test_no_context_nodes_means_only_write_source(self, tmp_path: Path) -> None:
        # Single-node file: no siblings exist, so no read_source is added.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        runner = StagingRunner(store)
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        session.run()
        keys = runner.assigned[0].context
        assert any(k.startswith("write_source:") for k in keys)
        assert not any(k.startswith("read_source:") for k in keys)

    def test_same_file_siblings_auto_enriched_without_planner_context(
        self, tmp_path: Path
    ) -> None:
        # When the planner specifies no context_nodes, the agent still receives
        # all same-file siblings as read_source so it is never blind to its own
        # file, regardless of what the planner decided.
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)
        runner = StagingRunner(store)
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        # Task targets only 'a', planner specifies NO context_nodes.
        session.install_plan([_task("a", ["m.py::function::a"])])
        session.run()
        ctx = runner.assigned[0].context
        # Write target is present under write_source.
        assert "write_source:m.py::function::a" in ctx
        # Sibling 'b' is auto-included as read_source even without planner hints.
        assert "read_source:m.py::function::b" in ctx
        # The sibling is read-only — not a write target.
        assert "write_source:m.py::function::b" not in ctx

    def test_cross_file_callers_auto_enriched(self, tmp_path: Path) -> None:
        # A node in a different file that references the target symbol by name
        # must be included as read_source so the agent understands its callers
        # across the whole codebase, not just within its own file.
        (tmp_path / "fruit").mkdir()
        (tmp_path / "animal").mkdir()
        (tmp_path / "fruit" / "main.py").write_text(
            "def apple():\n    return 1\n"
        )
        (tmp_path / "animal" / "main.py").write_text(
            "from fruit.main import apple\n\ndef dog():\n    return apple()\n"
        )
        store = _store(tmp_path)
        runner = StagingRunner(store)
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        # Task targets apple; planner specifies no context_nodes.
        session.install_plan(
            [_task("fix_apple", ["fruit/main.py::function::apple"])]
        )
        session.run()
        ctx = runner.assigned[0].context
        # apple is the write target.
        assert "write_source:fruit/main.py::function::apple" in ctx
        # dog (in a different file) calls apple — must be auto-included.
        assert any(
            "animal/main.py" in k and k.startswith("read_source:")
            for k in ctx
        )


class TestTransactionalCommit:
    """The store must not advance unless reconstruction is valid."""

    def test_invalid_staged_source_not_committed(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        original = (tmp_path / "m.py").read_text()
        # Agent stages a syntactically broken fragment.
        runner = StagingRunner(store, new_source="def broken(:\n")
        session = _session(
            tmp_path, runner=runner, node_store=store, max_attempts=1
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        assert not result.ok
        assert "a" in result.failed
        # Store stayed at v1 and the file on disk is untouched.
        assert store.get_node(NodeId("m.py::function::a")).version == 1
        assert (tmp_path / "m.py").read_text() == original

    def test_preview_gate_validates_assembled_file(self, tmp_path: Path) -> None:
        # Directly exercise the pre-commit preview: a staged fragment that would
        # assemble into invalid Python is rejected; a valid one passes.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        session = _session(tmp_path, runner=StagingRunner(store), node_store=store)
        session.initialize()
        nid = NodeId("m.py::function::a")

        store.put_node(nid, NodeFragment(nid, "function", "def broken(:\n", 1))
        assert session._preview_is_valid([nid]) is False

        store.rollback_node(nid)
        valid = NodeFragment(nid, "function", "def a():\n    return 9\n", 1)
        store.put_node(nid, valid)
        assert session._preview_is_valid([nid]) is True

    def test_reconstruct_failure_reverts_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        runner = StagingRunner(store)  # stages valid "x = 1\n"
        session = _session(
            tmp_path, runner=runner, node_store=store, max_attempts=1
        )
        session.initialize()

        def boom(nodes: object) -> list[str]:
            raise OSError("disk full")

        # A write failure AFTER commit must revert the store so disk and store
        # never diverge.
        monkeypatch.setattr(session, "_reconstruct_affected", boom)
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        assert not result.ok
        node = store.get_node(NodeId("m.py::function::a"))
        assert node.version == 1
        assert "return 0" in node.source


class TestStallReporting:
    """A stalled run must report FAILED + blocked, never COMPLETED."""

    def test_stalled_run_reports_failed_not_completed(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        lock_table = LockTable()
        session = _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            lock_table=lock_table,
        )
        session.initialize()
        # An external holder owns the only target node's lock forever (acquired after
        # initialize, which clears stale leases), so the task can never acquire it and
        # the run stalls with zero failures.
        lock_table.try_acquire(
            NodeId("m.py::function::a"), LockMode.WRITE, "external"
        )
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        assert result.state is SessionState.FAILED
        assert result.blocked == ("a",)
        assert result.skipped == ()
        assert not result.ok

    def test_dependent_of_failed_task_is_skipped_not_blocked(
        self, tmp_path: Path
    ) -> None:
        # 'a' always fails; 'b' depends on 'a' and 'c' depends on 'b'. Both
        # downstream tasks are reported as *skipped* (a failed ancestor), and the
        # genuinely-blocked list stays empty.
        (tmp_path / "m.py").write_text(
            "def a():\n    return 0\n\n"
            "def b():\n    return 0\n\n"
            "def c():\n    return 0\n"
        )
        store = _store(tmp_path)
        runner = StagingRunner(store, coverage={"a": 0.0})  # 'a' never passes
        session = _session(
            tmp_path, runner=runner, node_store=store, max_attempts=2
        )
        session.initialize()
        session.install_plan([
            _task("a", ["m.py::function::a"]),
            _task("b", ["m.py::function::b"], deps=["a"]),
            _task("c", ["m.py::function::c"], deps=["b"]),
        ])
        result = session.run()
        assert result.state is SessionState.FAILED
        assert result.failed == ("a",)
        assert set(result.skipped) == {"b", "c"}
        assert result.blocked == ()
        assert not result.ok


class TestDefaultAgentRouting:
    def _session_with_default(
        self, tmp_path: Path, store: NodeStore, default: str | None
    ) -> Session:
        return Session(
            session_id="s1",
            config=_config(tmp_path),
            node_store=store,
            lock_table=LockTable(),
            registry=FakeRegistry(),  # type: ignore[arg-type]
            agent_runner=StagingRunner(store),  # type: ignore[arg-type]
            default_agent_type=default,
        )

    def test_empty_agent_type_routed_to_default(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        session = self._session_with_default(tmp_path, store, "anthropic_api")
        session.initialize()
        bare = SubTask(
            task_id="a",
            description="task a",
            target_nodes=[NodeId("m.py::function::a")],
        )
        assert bare.agent_type == ""
        session.install_plan([bare])
        assert session._dag_task("a").agent_type == "anthropic_api"

    def test_explicit_agent_type_preserved(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        session = self._session_with_default(tmp_path, store, "anthropic_api")
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        assert session._dag_task("a").agent_type == "fake"

    def test_no_default_leaves_agent_type_unchanged(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        session = self._session_with_default(tmp_path, store, None)
        session.initialize()
        bare = SubTask(
            task_id="a",
            description="task a",
            target_nodes=[NodeId("m.py::function::a")],
        )
        session.install_plan([bare])
        assert session._dag_task("a").agent_type == ""


# --- Wave 5: concurrency -----------------------------------------------------


class _SlowStagingRunner:
    """A staging agent that blocks for ``delay`` seconds before returning."""

    def __init__(self, node_store: NodeStore, delay: float) -> None:
        self._store = node_store
        self._delay = delay

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        time.sleep(self._delay)
        done: list[NodeId] = []
        for node_id in task.target_nodes:
            self._store.put_node(
                node_id, NodeFragment(node_id, "function", "x = 1\n", 1)
            )
            done.append(node_id)
        return TaskResult(
            task_id=task.task_id, success=True, modified_nodes=list(done)
        )


class TestConcurrentDispatch:
    def test_two_independent_tasks_both_complete(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)
        runner = StagingRunner(store)
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan(
            [
                _task("a", ["m.py::function::a"]),
                _task("b", ["m.py::function::b"]),
            ]
        )
        result = session.run()
        assert result.ok
        assert set(result.completed) == {"a", "b"}
        # Both edits landed on disk.
        rebuilt = (tmp_path / "m.py").read_text()
        compile(rebuilt, "m.py", "exec")


class TestCrossAgentConflictDetection:
    """The headline Wave 5 behavior: one batch, one multi-task EditRound."""

    def test_batch_detects_cross_agent_name_collision(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)
        lock_table = LockTable()
        session = _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            lock_table=lock_table,
            max_attempts=1,
        )
        session.initialize()
        na = NodeId("m.py::function::a")
        nb = NodeId("m.py::function::b")
        session.install_plan([_task("a", [str(na)]), _task("b", [str(nb)])])

        # Both agents, completing in the *same batch*, introduce a function named
        # 'helper' into m.py — a genuine cross-agent collision.
        lock_table.try_acquire_all([(na, LockMode.WRITE)], "a")
        lock_table.try_acquire_all([(nb, LockMode.WRITE)], "b")
        helper1 = "def helper():\n    return 1\n"
        helper2 = "def helper():\n    return 2\n"
        store.put_node(na, NodeFragment(na, "function", helper1, 1))
        store.put_node(nb, NodeFragment(nb, "function", helper2, 1))
        batch = [
            _Completion(
                TaskBundle(task_id="a", description="", target_nodes=[na]),
                TaskResult(task_id="a", success=True, modified_nodes=[na]),
            ),
            _Completion(
                TaskBundle(task_id="b", description="", target_nodes=[nb]),
                TaskResult(task_id="b", success=True, modified_nodes=[nb]),
            ),
        ]
        session._process_batch(batch)

        # Deterministic order: 'a' commits first; 'b' collides with the now-committed
        # 'helper' and is rejected — the detector saw a cross-agent edit at last.
        assert session._completed == ["a"]
        assert session._failed == ["b"]
        assert "helper" in store.get_node(na).source
        # 'b' was rolled back to the ingested definition.
        assert store.get_node(nb).source.lstrip().startswith("def b")


class TestCommitTimeLockRevalidation:
    """RA-3: never commit through a write lock that was reclaimed mid-call."""

    def test_commit_aborts_when_write_lock_not_held(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        session = _session(
            tmp_path, runner=StagingRunner(store), node_store=store, max_attempts=1
        )
        session.initialize()
        nid = NodeId("m.py::function::a")
        session.install_plan([_task("a", [str(nid)])])
        # Stage a valid edit but hold no write lock (a lapsed lease).
        edited = "def a():\n    return 9\n"
        store.put_node(nid, NodeFragment(nid, "function", edited, 1))
        committed = session._validate_and_commit("a", [nid])
        assert committed == []
        assert store.get_node(nid).version == 1  # store never advanced

    def test_heartbeat_keeps_slow_agent_lease_alive(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        lock_table = LockTable(default_timeout=0.4)  # lease lapses after 0.4s
        session = Session(
            session_id="s1",
            config=_config(tmp_path),
            node_store=store,
            lock_table=lock_table,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            agent_runner=_SlowStagingRunner(store, delay=0.8),  # type: ignore[arg-type]  # outlives the lease
            heartbeat_interval_s=0.1,
            max_attempts=1,
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        # The heartbeat renewed the lease past the timeout, so the commit owned it.
        assert result.ok
        assert "x = 1" in (tmp_path / "m.py").read_text()

    def test_expired_lease_without_heartbeat_fails_commit(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        lock_table = LockTable(default_timeout=0.4)
        session = Session(
            session_id="s1",
            config=_config(tmp_path),
            node_store=store,
            lock_table=lock_table,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            agent_runner=_SlowStagingRunner(store, delay=0.8),  # type: ignore[arg-type]
            heartbeat_interval_s=100.0,  # never fires during the run
            max_attempts=1,
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        # No heartbeat: the lease expired mid-call, so the commit was refused.
        assert not result.ok
        assert result.failed == ("a",)
        assert store.get_node(NodeId("m.py::function::a")).version == 1


# --- Wave 6: agent source transport ------------------------------------------


class WireRunner:
    """An API-shaped agent: returns rewritten source over the wire (no put_node).

    This mirrors what a real ``anthropic_api`` agent does — it never touches the
    node store; it reports ``new_sources`` and the session stages them. ``sources``
    maps node id -> the full rewritten source for that node.
    """

    def __init__(self, sources: dict[str, str]) -> None:
        self._sources = sources

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        new = {
            node_id: self._sources[str(node_id)]
            for node_id in task.target_nodes
            if str(node_id) in self._sources
        }
        return TaskResult(
            task_id=task.task_id,
            success=True,
            modified_nodes=list(new),
            new_sources=new,
        )


class TestGreenfieldWithGit:
    """The user's scenario: build new files into a dir that is not its own repo."""

    def _git_session(self, work: Path, runner: object) -> Session:
        config = MakConfig(
            session=SessionConfig(work_dir=str(work), mak_dir=str(work / ".mak")),
            git=GitConfig(auto_commit=True, auto_push=False),
            node_store=NodeStoreConfig(),
        )
        return Session(
            session_id="s1",
            config=config,
            node_store=NodeStore(work / ".mak" / "ns"),
            lock_table=LockTable(),
            registry=FakeRegistry(),  # type: ignore[arg-type]
            agent_runner=runner,  # type: ignore[arg-type]
            git_helper=GitHelper(work),
        )

    def test_greenfield_file_created_and_committed_in_own_repo(
        self, tmp_path: Path
    ) -> None:
        # tmp_path is sat inside an OUTER repo, mirroring a work-dir nested in a home
        # repo. MAK must give the work-dir its own repo and commit there, not leak
        # into the outer one.
        outer = tmp_path / "outer"
        outer.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
        work = outer / "project"
        work.mkdir()

        runner = WireRunner({"app/main.py": "def main():\n    return 0\n"})
        session = self._git_session(work, runner)
        session.initialize()  # should `git init` work/ (it is not its own repo root)
        session.install_plan([_task("core", ["app/main.py"])])
        result = session.run()

        assert result.ok
        assert (work / "app" / "main.py").exists()
        # work/ is now its own repo, with a MAK audit commit for the new file.
        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=work, check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert Path(toplevel).resolve() == work.resolve()
        log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=work, check=True, capture_output=True, text=True,
        ).stdout
        assert "core" in log
        # The outer repo saw none of it.
        outer_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=outer, check=True, capture_output=True, text=True,
        ).stdout
        assert "app/main.py" not in outer_status


class TestSourceTransport:
    def test_agent_returned_source_is_applied(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        runner = WireRunner({"m.py::function::a": "def a():\n    return 99\n"})
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        assert result.ok
        # The source the agent sent over the wire reached the store and disk.
        assert "return 99" in store.get_node(NodeId("m.py::function::a")).source
        assert "return 99" in (tmp_path / "m.py").read_text()

    def test_concurrent_wire_agents_all_apply(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)
        runner = WireRunner(
            {
                "m.py::function::a": "def a():\n    return 1\n",
                "m.py::function::b": "def b():\n    return 2\n",
            }
        )
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan(
            [_task("a", ["m.py::function::a"]), _task("b", ["m.py::function::b"])]
        )
        result = session.run()
        assert result.ok
        assert set(result.completed) == {"a", "b"}
        rebuilt = (tmp_path / "m.py").read_text()
        assert "return 1" in rebuilt and "return 2" in rebuilt

    def test_greenfield_whole_file_node_is_created(self, tmp_path: Path) -> None:
        # Greenfield: a bare-path node ("editor/main.py", no ::kind::name) is a whole
        # new file the agent returns in full. It must be created on disk, in a new
        # subdirectory, and the task must complete.
        store = _store(tmp_path)
        source = "import sys\n\n\ndef main():\n    return 0\n"
        runner = WireRunner({"editor/main.py": source})
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("core", ["editor/main.py"])])
        result = session.run()
        assert result.ok
        assert result.completed == ("core",)
        created = tmp_path / "editor" / "main.py"
        assert created.exists()
        assert "def main():" in created.read_text()
        assert store.get_node(NodeId("editor/main.py")).kind == "module"

    def test_greenfield_multiple_new_files(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = WireRunner({
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper():\n    return 1\n",
            "app.py": (
                "from pkg.util import helper\n\n\ndef run():\n    return helper()\n"
            ),
        })
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([
            _task("init", ["pkg/__init__.py"]),
            _task("util", ["pkg/util.py"]),
            _task("app", ["app.py"], deps=["util"]),
        ])
        result = session.run()
        assert result.ok
        assert (tmp_path / "pkg" / "util.py").exists()
        assert "def helper" in (tmp_path / "pkg" / "util.py").read_text()
        assert "def run" in (tmp_path / "app.py").read_text()

    def test_whole_file_rewrite_of_existing_file_is_not_doubled(
        self, tmp_path: Path
    ) -> None:
        # An existing file is ingested as fragments; a task that targets the whole
        # file (bare path) and returns the full new source must REPLACE it, not append
        # to the old fragments (which doubled every top-level symbol before the fix).
        (tmp_path / "m.py").write_text(
            "def a():\n    return 0\n\n\ndef b():\n    return 0\n"
        )
        store = _store(tmp_path)
        new_source = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        runner = WireRunner({"m.py": new_source})
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("rewrite", ["m.py"])])
        result = session.run()
        assert result.ok
        rebuilt = (tmp_path / "m.py").read_text()
        assert rebuilt.count("def a(") == 1  # not doubled
        assert rebuilt.count("def b(") == 1
        assert "return 1" in rebuilt and "return 2" in rebuilt

    def test_noop_audit_of_existing_file_completes(self, tmp_path: Path) -> None:
        # An "audit" task targeting an existing file whose agent finds nothing to
        # change (success=True, no modified_nodes, no sources) must COMPLETE — the
        # file is already correct — not retry to failure.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)

        class NoOpRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(task_id=task.task_id, success=True)

        session = _session(tmp_path, runner=NoOpRunner(), node_store=store)
        session.initialize()
        session.install_plan([_task("audit", ["m.py"])])
        result = session.run()
        assert result.ok
        assert result.completed == ("audit",)
        # The file is untouched and still valid.
        assert (tmp_path / "m.py").read_text() == "def a():\n    return 0\n"

    def test_noop_create_of_missing_file_still_fails(self, tmp_path: Path) -> None:
        # The no-op acceptance must NOT mask a real miss: a create task whose target
        # does not exist and that returns nothing has produced nothing, so it fails.
        store = _store(tmp_path)

        class NoOpRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(task_id=task.task_id, success=True)

        session = _session(
            tmp_path, runner=NoOpRunner(), node_store=store, max_attempts=2
        )
        session.initialize()
        session.install_plan([_task("create", ["new.py"])])
        result = session.run()
        assert result.failed == ("create",)
        assert not (tmp_path / "new.py").exists()

    def test_claimed_node_without_source_fails_cleanly(self, tmp_path: Path) -> None:
        # A misbehaving agent: success=True, claims it changed a node, but sends no
        # source and stages nothing. Must not crash the commit phase; the task is
        # simply not applied and fails after its attempts are exhausted.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)

        class HollowRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    modified_nodes=list(task.target_nodes),
                )

        session = _session(
            tmp_path, runner=HollowRunner(), node_store=store, max_attempts=1
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        assert not result.ok
        assert "a" in result.failed
        assert store.get_node(NodeId("m.py::function::a")).version == 1
        assert (tmp_path / "m.py").read_text() == "def a():\n    return 0\n"

    def test_out_of_scope_source_is_ignored(self, tmp_path: Path) -> None:
        # An agent for task 'a' also tries to rewrite node 'b', outside its grant.
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)

        class OverreachRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    modified_nodes=[NodeId("m.py::function::a")],
                    new_sources={
                        NodeId("m.py::function::a"): "def a():\n    return 1\n",
                        NodeId("m.py::function::b"): "def b():\n    return 666\n",
                    },
                )

        session = _session(tmp_path, runner=OverreachRunner(), node_store=store)
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])  # only 'a' granted
        result = session.run()
        assert result.ok
        assert "return 1" in store.get_node(NodeId("m.py::function::a")).source
        # The out-of-scope edit to 'b' was never staged or written.
        assert "666" not in store.get_node(NodeId("m.py::function::b")).source
        assert "666" not in (tmp_path / "m.py").read_text()
