"""Tests for mak.session: lifecycle, dispatch, partial completion, recovery."""

from __future__ import annotations

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
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.session import Session, SessionState, SubTaskProgress

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
        # Drive only the first task to completion, then stop (crash).
        s1._scheduler.tick()  # dispatch a
        bundle, result = s1._pending_results.pop(0)
        s1._process_result(bundle, result)
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
        # An external holder owns the only target node's lock forever, so the
        # task can never acquire it and the run stalls with zero failures.
        lock_table.try_acquire(
            NodeId("m.py::function::a"), LockMode.WRITE, "external"
        )
        session = _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            lock_table=lock_table,
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()
        assert result.state is SessionState.FAILED
        assert result.blocked == ("a",)
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
