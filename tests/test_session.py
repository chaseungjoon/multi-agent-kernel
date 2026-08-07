"""Tests for mak.session: lifecycle, dispatch, partial completion, recovery."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from mak.config import GitConfig, MakConfig, NodeStoreConfig, SessionConfig
from mak.core.exceptions import SessionError
from mak.core.logging import EventType, SessionLogger
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
    config: MakConfig | None = None,
    logger: SessionLogger | None = None,
) -> Session:
    return Session(
        session_id="s1",
        config=config or _config(tmp_path),
        node_store=node_store,
        lock_table=lock_table or LockTable(),
        registry=FakeRegistry(),  # type: ignore[arg-type]
        agent_runner=runner,
        git_helper=git_helper,  # type: ignore[arg-type]
        test_runner=test_runner,  # type: ignore[arg-type]
        logger=logger,
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


class TestPlanValidation:
    """Wave 10: install_plan grounds and augments plans against the code graph."""

    def _project(self, tmp_path: Path) -> NodeStore:
        (tmp_path / "util.py").write_text("def parse_config(s):\n    return s\n")
        (tmp_path / "app.py").write_text(
            "from util import parse_config\n\n\n"
            "def load():\n    return parse_config('x')\n"
        )
        return _store(tmp_path)

    def _session_validate(
        self, tmp_path: Path, store: NodeStore, *, validate: bool
    ) -> Session:
        from dataclasses import replace as _replace
        base = _config(tmp_path)
        config = _replace(base, planner=_replace(base.planner, validate=validate))
        return Session(
            session_id="s1", config=config, node_store=store,
            lock_table=LockTable(), registry=FakeRegistry(),  # type: ignore[arg-type]
            agent_runner=StagingRunner(store),  # type: ignore[arg-type]
        )

    def test_missing_edge_added_at_install(self, tmp_path: Path) -> None:
        store = self._project(tmp_path)
        session = self._session_validate(tmp_path, store, validate=True)
        session.initialize()
        # app.load references util.parse_config, but the plan omits the edge.
        session.install_plan([
            _task("edit-parse", ["util.py::function::parse_config"]),
            _task("edit-load", ["app.py::function::load"]),
        ])
        assert session._dag_task("edit-load").depends_on == ["edit-parse"]
        kinds = {f.kind for f in session.last_plan_findings}
        assert "missing_dep" in kinds

    def test_hallucinated_target_corrected(self, tmp_path: Path) -> None:
        store = self._project(tmp_path)
        session = self._session_validate(tmp_path, store, validate=True)
        session.initialize()
        # 'lod' is a typo for the real 'load'.
        session.install_plan([_task("t", ["app.py::function::lod"])])
        assert session._dag_task("t").target_nodes == [
            NodeId("app.py::function::load")
        ]
        assert "corrected_node" in {f.kind for f in session.last_plan_findings}

    def test_validate_false_is_identity(self, tmp_path: Path) -> None:
        store = self._project(tmp_path)
        session = self._session_validate(tmp_path, store, validate=False)
        session.initialize()
        session.install_plan([
            _task("edit-parse", ["util.py::function::parse_config"]),
            _task("edit-load", ["app.py::function::load"]),
        ])
        # No validation: the missing edge is NOT added and no findings recorded.
        assert session._dag_task("edit-load").depends_on == []
        assert session.last_plan_findings == []

    def test_cascade_tasks_validate_without_findings(self, tmp_path: Path) -> None:
        # Cascade tasks target real inventory ids, so validation is a clean no-op.
        store = self._project(tmp_path)
        session = self._session_validate(tmp_path, store, validate=True)
        session.initialize()
        session.install_plan([_task("solo", ["util.py::function::parse_config"])])
        assert session.last_plan_findings == []


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


class TestCascadeDetection:
    """detect_cascade_tasks() identifies callers broken by signature changes."""

    def test_no_cascade_when_body_only_changes(self, tmp_path: Path) -> None:
        # Changing a function body (same signature) must not trigger cascades.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        runner = StagingRunner(store, new_source="def a():\n    return 99\n")
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        session.run()
        assert session.detect_cascade_tasks() == []

    def test_cascade_tasks_for_cross_file_callers(self, tmp_path: Path) -> None:
        # When apple's signature gains a new parameter, dog (in another file)
        # calls the old signature and must be included in a cascade wave.
        (tmp_path / "fruit").mkdir()
        (tmp_path / "animal").mkdir()
        (tmp_path / "fruit" / "main.py").write_text(
            "def apple(x):\n    return x\n"
        )
        (tmp_path / "animal" / "main.py").write_text(
            "from fruit.main import apple\n\ndef dog():\n    return apple(1)\n"
        )
        store = _store(tmp_path)
        runner = StagingRunner(
            store, new_source="def apple(x, y):\n    return x + y\n"
        )
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan(
            [_task("fix_apple", ["fruit/main.py::function::apple"])]
        )
        session.run()

        cascade = session.detect_cascade_tasks()
        assert cascade, "expected cascade tasks after signature change"
        # At least one cascade task should target a node in animal/main.py.
        assert any(
            "animal/main.py" in str(t.target_nodes[0]) for t in cascade
        )
        # Context node should point back to the changed function.
        assert any("fruit/main.py" in str(c) for t in cascade for c in t.context_nodes)

    def test_no_cascade_for_brand_new_function(self, tmp_path: Path) -> None:
        # A new function has no prior callers; no cascade tasks expected.
        # We simulate this by changing body only and verifying the new-node path.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        # Stage a non-function so _extract_sig returns None → no cascade.
        runner = StagingRunner(store, new_source="x = 1\n")
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        session.run()
        assert session.detect_cascade_tasks() == []

    def test_install_plan_from_completed_enables_cascade_wave(
        self, tmp_path: Path
    ) -> None:
        # install_plan() must succeed after COMPLETED so a cascade wave can be
        # installed and run without re-initializing the session.
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)
        runner = StagingRunner(store)
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("first", ["m.py::function::a"])])
        result1 = session.run()
        assert result1.ok
        # Cascade wave: install a second plan from COMPLETED state.
        session.install_plan([_task("cascade", ["m.py::function::b"])])
        result2 = session.run()
        assert result2.ok
        # First wave's completed list was reset; only cascade task is reported.
        assert "cascade" in result2.completed
        assert "first" not in result2.completed


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


class _ListingRegistry:
    """A registry that can enumerate its configured agent types."""

    def __init__(self, types: list[str]) -> None:
        self._types = types

    def get(self, agent_type: str) -> FakeAdapter:
        return FakeAdapter()

    def list_types(self) -> list[str]:
        return list(self._types)


class TestAgentDistribution:
    def _session(self, tmp_path: Path, store: NodeStore, pool: list[str]) -> Session:
        return Session(
            session_id="s1",
            config=_config(tmp_path),
            node_store=store,
            lock_table=LockTable(),
            registry=_ListingRegistry(pool),  # type: ignore[arg-type]
            agent_runner=StagingRunner(store),  # type: ignore[arg-type]
            default_agent_type=pool[0],
            agent_pool=pool,
        )

    def test_empty_types_distributed_round_robin(self, tmp_path: Path) -> None:
        src = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(4))
        (tmp_path / "m.py").write_text(src)
        store = _store(tmp_path)
        pool = ["anthropic_api", "openai_api", "gemini_api"]
        session = self._session(tmp_path, store, pool)
        session.initialize()
        tasks = [
            SubTask(
                task_id=f"t{i}",
                description="x",
                target_nodes=[NodeId(f"m.py::function::f{i}")],
            )
            for i in range(4)
        ]
        session.install_plan(tasks)
        assigned = [session._dag_task(f"t{i}").agent_type for i in range(4)]
        # Round-robin across the pool, wrapping on the 4th task.
        assert assigned == [
            "anthropic_api", "openai_api", "gemini_api", "anthropic_api"
        ]

    def test_unconfigured_type_remapped_not_crashing(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        pool = ["anthropic_api", "openai_api"]
        session = self._session(tmp_path, store, pool)
        session.initialize()
        bad = SubTask(
            task_id="a",
            description="x",
            target_nodes=[NodeId("m.py::function::a")],
            agent_type="hallucinated_backend",
        )
        session.install_plan([bad])
        # Remapped to the pool's first entry instead of crashing dispatch.
        assert session._dag_task("a").agent_type == "anthropic_api"


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


class TestPlanMetrics:
    """Wave 10: realized-parallelism and rework metrics on the SessionResult."""

    def test_two_parallel_tasks_report_max_concurrency(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(_TWO_FUNCS)
        store = _store(tmp_path)
        session = _session(
            tmp_path, runner=_SlowStagingRunner(store, 0.15), node_store=store
        )
        session.initialize()
        session.install_plan(
            [
                _task("a", ["m.py::function::a"]),
                _task("b", ["m.py::function::b"]),
            ]
        )
        result = session.run()
        assert result.ok
        # Two independent tasks ran at once: the scheduler sampled >= 2 in flight.
        assert result.metrics["max_concurrency"] >= 2

    def test_conflict_rejection_counted(self, tmp_path: Path) -> None:
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
        # Both agents introduce a 'helper' name in one batch: 'b' is rejected.
        lock_table.try_acquire_all([(na, LockMode.WRITE)], "a")
        lock_table.try_acquire_all([(nb, LockMode.WRITE)], "b")
        h1 = "def helper():\n    return 1\n"
        h2 = "def helper():\n    return 2\n"
        store.put_node(na, NodeFragment(na, "function", h1, 1))
        store.put_node(nb, NodeFragment(nb, "function", h2, 1))
        session._process_batch(
            [
                _Completion(
                    TaskBundle(task_id="a", description="", target_nodes=[na]),
                    TaskResult(task_id="a", success=True, modified_nodes=[na]),
                ),
                _Completion(
                    TaskBundle(task_id="b", description="", target_nodes=[nb]),
                    TaskResult(task_id="b", success=True, modified_nodes=[nb]),
                ),
            ]
        )
        assert session._plan_metrics()["conflict_rejections"] == 1

    def test_plan_metrics_logged_and_readable(self, tmp_path: Path) -> None:
        from mak.core.logging import EventType, SessionLogger

        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        logger = SessionLogger(tmp_path / "session.jsonl")
        session = Session(
            session_id="s1",
            config=_config(tmp_path),
            node_store=store,
            lock_table=LockTable(),
            registry=FakeRegistry(),  # type: ignore[arg-type]
            agent_runner=StagingRunner(store),  # type: ignore[arg-type]
            logger=logger,
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        session.run()

        metric_entries = [
            e for e in logger.read_log() if e.event_type is EventType.PLAN_METRICS
        ]
        assert len(metric_entries) == 1
        payload = metric_entries[0].payload
        assert payload["tasks_completed"] == 1
        assert "max_concurrency" in payload
        assert "redispatches" in payload


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

    def test_whole_file_rewrite_with_a_future_import_is_accepted(
        self, tmp_path: Path
    ) -> None:
        # The live failure the sibling test above could not catch: its rewrite
        # happened to compile even when the preview doubled the file, so the gate
        # let it through and `commit_node` cleaned up afterwards. A rewrite opening
        # with `from __future__` cannot — doubled, that import lands mid-file and
        # compile() rejects it, so the task was rejected with "reconstruction would
        # produce invalid Python" on every attempt and lost ~40 KB of real work
        # each time. The preview must supersede the fragments, as the commit does.
        (tmp_path / "m.py").write_text(
            "from __future__ import annotations\n\n\ndef a():\n    return 0\n"
        )
        store = _store(tmp_path)
        new_source = (
            '"""Rewritten."""\n\nfrom __future__ import annotations\n\n'
            "import sys\n\n\ndef a():\n    return sys.maxsize\n"
        )
        runner = WireRunner({"m.py": new_source})
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("rewrite", ["m.py"])])
        result = session.run()
        assert result.ok, result.failure_reasons
        assert result.completed == ("rewrite",)
        rebuilt = (tmp_path / "m.py").read_text()
        assert rebuilt.count("from __future__ import annotations") == 1
        assert "sys.maxsize" in rebuilt

    def test_noop_audit_of_existing_file_completes(self, tmp_path: Path) -> None:
        # An "audit" task targeting an existing file whose agent finds nothing to
        # change must COMPLETE — the file is already correct — not retry to
        # failure. Rewritten for Wave 12: the agent now has to *assert* the no-op
        # (no_changes_required), because "success with no fragments" is also
        # exactly what a reply cut off at the output-token limit looks like, and
        # that shape was closing tasks on which no work had been done.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)

        class NoOpRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id, success=True, no_changes_required=True
                )

        session = _session(tmp_path, runner=NoOpRunner(), node_store=store)
        session.initialize()
        session.install_plan([_task("audit", ["m.py"])])
        result = session.run()
        assert result.ok
        assert result.completed == ("audit",)
        # Reported apart from work that actually happened.
        assert result.noop == ("audit",)
        assert result.metrics["tasks_noop"] == 1.0
        # The file is untouched and still valid.
        assert (tmp_path / "m.py").read_text() == "def a():\n    return 0\n"

    def test_unasserted_empty_success_does_not_complete(self, tmp_path: Path) -> None:
        # 12.2 / 12.5b, the defect this wave exists for: `marks` and `modes` were
        # reported completed on exactly this shape — success, no fragments, target
        # already on disk — having received no work at all.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)

        class EmptyRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(task_id=task.task_id, success=True)

        session = _session(
            tmp_path, runner=EmptyRunner(), node_store=store, max_attempts=2
        )
        session.initialize()
        session.install_plan([_task("extend", ["m.py"])])
        result = session.run()
        assert result.failed == ("extend",)
        assert result.completed == ()
        assert result.metrics["tasks_completed"] == 0.0
        assert "did not assert" in result.failure_reasons["extend"]

    def test_noop_assertion_does_not_mask_a_missing_target(
        self, tmp_path: Path
    ) -> None:
        # The assertion is about the *work*, not about the file: claiming "nothing
        # to change" for a file that does not exist proves nothing.
        store = _store(tmp_path)

        class AssertedNoOpRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id, success=True, no_changes_required=True
                )

        session = _session(
            tmp_path, runner=AssertedNoOpRunner(), node_store=store, max_attempts=2
        )
        session.initialize()
        session.install_plan([_task("create", ["new.py"])])
        result = session.run()
        assert result.failed == ("create",)

    def test_truncated_result_fails_with_a_named_reason(self, tmp_path: Path) -> None:
        # A truncation reaches the session as a failed result carrying the stop
        # reason; the run must say so rather than reporting a vague empty result.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)

        class TruncatedRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    stop_reason="max_tokens",
                )

        session = _session(
            tmp_path, runner=TruncatedRunner(), node_store=store, max_attempts=2
        )
        session.initialize()
        session.install_plan([_task("extend", ["m.py"])])
        result = session.run()
        assert result.failed == ("extend",)
        assert "output-token limit" in result.failure_reasons["extend"]

    def test_a_retry_after_truncation_differs_from_the_first_attempt(
        self, tmp_path: Path
    ) -> None:
        # 12.3a: three identical agent_result events for one task can no longer
        # happen — the re-dispatch carries why the last attempt produced nothing.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        notes: list[str | None] = []

        class TruncatedRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                notes.append(task.retry_note)
                return TaskResult(
                    task_id=task.task_id, success=True, stop_reason="max_tokens"
                )

        session = _session(
            tmp_path, runner=TruncatedRunner(), node_store=store, max_attempts=3
        )
        session.initialize()
        session.install_plan([_task("extend", ["m.py"])])
        session.run()
        assert notes[0] is None
        assert notes[1] is not None
        assert "cut off" in notes[1]

    def test_a_task_failing_differently_each_attempt_reports_every_reason(
        self, tmp_path: Path
    ) -> None:
        # A real run rejected attempts 1-2 on one underlying defect and then hit a
        # one-off malformed response on attempt 3 — and reported only attempt 3,
        # which named nothing relevant to the actual problem. Every distinct
        # reason is now reported, in the order first seen.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        errors = ["first cause", "first cause", "unrelated one-off"]

        class VaryingRunner:
            def __init__(self) -> None:
                self.n = 0

            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                error = errors[min(self.n, len(errors) - 1)]
                self.n += 1
                return TaskResult(task_id=task.task_id, success=False, error=error)

        session = _session(
            tmp_path, runner=VaryingRunner(), node_store=store, max_attempts=3
        )
        session.initialize()
        session.install_plan([_task("t", ["m.py"])])
        result = session.run()
        assert result.failed == ("t",)
        reason = result.failure_reasons["t"]
        assert "first cause" in reason
        assert "unrelated one-off" in reason
        assert "3 attempts failed for 2 reasons" in reason

    def test_a_single_recurring_reason_is_reported_plainly(
        self, tmp_path: Path
    ) -> None:
        # The common case must not gain noise: one reason stays one sentence.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)

        class AlwaysFailsRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(
                    task_id=task.task_id, success=False, error="the same cause"
                )

        session = _session(
            tmp_path, runner=AlwaysFailsRunner(), node_store=store, max_attempts=3
        )
        session.initialize()
        session.install_plan([_task("t", ["m.py"])])
        result = session.run()
        assert result.failure_reasons["t"] == "the same cause"

    def test_a_non_retryable_result_fails_without_spending_attempts(
        self, tmp_path: Path
    ) -> None:
        # 12.3c: a refusal earns the same refusal every time, so it must not
        # consume the whole attempt budget.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        calls: list[str] = []

        class RefusingRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                calls.append(task.task_id)
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error="model declined",
                    stop_reason="refusal",
                    retryable=False,
                )

        session = _session(
            tmp_path, runner=RefusingRunner(), node_store=store, max_attempts=3
        )
        session.initialize()
        session.install_plan([_task("extend", ["m.py"])])
        result = session.run()
        assert result.failed == ("extend",)
        assert len(calls) == 1
        assert "not retryable" in result.failure_reasons["extend"]

    def test_noop_rejected_when_file_has_syntax_error(self, tmp_path: Path) -> None:
        # The no-op acceptance MUST be blocked when the target file currently has
        # a syntax error. This is the keybindings bug: agent returns success+no-
        # changes, MAK silently accepts it as "done", file is never fixed.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        store.parse_file_into_nodes("m.py", "def a():\n    return 0\n")
        # Force-commit a broken version of the function — simulates a file that
        # has a syntax error in its committed state (e.g. a misplaced import).
        nid = NodeId("m.py::function::a")
        # put_node + commit_node to corrupt the committed source.
        store.put_node(nid, NodeFragment(nid, "function", "def a(:\n    broken\n", 1))
        store.commit_node(nid)

        class NoOpRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(task_id=task.task_id, success=True)

        session = _session(
            tmp_path, runner=NoOpRunner(), node_store=store, max_attempts=2
        )
        # Do NOT call session.initialize() — store is already populated above.
        session.state = SessionState.INITIALIZED  # skip ingestion
        session.install_plan([_task("fix", ["m.py::function::a"])])
        result = session.run()
        # No-op should be REJECTED because the file has a syntax error; the task
        # must fail (not silently complete) so the user knows MAK didn't fix it.
        assert result.failed == ("fix",)

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


# --- Wave 11.2: node-store self-pollution ------------------------------------


def _config_with_excludes(tmp_path: Path, patterns: tuple[str, ...]) -> MakConfig:
    return MakConfig(
        session=SessionConfig(work_dir=str(tmp_path), mak_dir=str(tmp_path / ".mak")),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(exclude_patterns=patterns),
    )


class TestStoreSelfPollution:
    """The store persists fragments as .py files; ingesting them compounds."""

    @staticmethod
    def _poison_store_dir(tmp_path: Path) -> Path:
        """Write a .mak/node_store/ tree like a previous run would leave."""
        frag = tmp_path / ".mak" / "node_store" / "editor" / "modes.py" / "v1.py"
        frag.parent.mkdir(parents=True, exist_ok=True)
        frag.write_text("def leaked():\n    return 1\n")
        return frag

    def test_store_dir_is_skipped_even_with_excludes_overridden(
        self, tmp_path: Path
    ) -> None:
        # 11.2b/e: proving exclusion *by construction*, not by pattern — the
        # exclude list here never mentions .mak, as a user override might not.
        (tmp_path / "real.py").write_text("def real():\n    return 1\n")
        self._poison_store_dir(tmp_path)
        store = NodeStore(tmp_path / "store")
        session = _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            config=_config_with_excludes(tmp_path, ("**/never_matches/**",)),
        )
        inventory = session.initialize()
        assert any("real.py" in str(n) for n in inventory)
        assert not any(str(n).startswith(".mak") for n in inventory)

    def test_node_count_does_not_drift_across_runs(self, tmp_path: Path) -> None:
        # The acceptance check: with the store living under the work dir, repeated
        # runs over an unchanged repo must report a stable node_count.
        (tmp_path / "real.py").write_text(_TWO_FUNCS)
        counts = []
        for _ in range(3):
            store = NodeStore(tmp_path / ".mak" / "node_store")
            session = _session(
                tmp_path, runner=StagingRunner(store), node_store=store
            )
            counts.append(len(session.initialize()))
        assert counts[0] > 0
        assert counts[0] == counts[1] == counts[2]

    def test_startup_prunes_a_store_poisoned_by_an_earlier_run(
        self, tmp_path: Path
    ) -> None:
        # 11.2d: a fix-forward run must not keep carrying the junk nodes an
        # earlier MAK already ingested from its own store.
        store = NodeStore(tmp_path / ".mak" / "node_store")
        store.parse_file_into_nodes(
            ".mak/node_store/editor/modes.py/v1.py", "def leaked():\n    return 1\n"
        )
        assert store.list_all_nodes()
        (tmp_path / "real.py").write_text("def real():\n    return 1\n")
        logger = SessionLogger(tmp_path / "session.log")
        session = _session(
            tmp_path, runner=StagingRunner(store), node_store=store, logger=logger
        )
        inventory = session.initialize()
        assert any("real.py" in str(n) for n in inventory)
        assert not any(".mak" in str(n) for n in store.list_all_nodes())
        (started,) = [
            e for e in logger.read_log() if e.event_type is EventType.SESSION_STARTED
        ]
        assert started.payload["pruned_nodes"] == 1


# --- Wave 11.3: dropped results are loud and diagnosable ---------------------


class _StrayRunner:
    """An agent that returns source for a node it was never granted."""

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        stray = NodeId("other.py::function::x")
        return TaskResult(
            task_id=task.task_id,
            success=True,
            modified_nodes=[stray],
            new_sources={stray: "def x():\n    return 1\n"},
        )


class TestAgentResultDiagnostics:
    def test_out_of_grant_source_is_logged_with_id_and_grant(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        logger = SessionLogger(tmp_path / "session.log")
        session = _session(
            tmp_path,
            runner=_StrayRunner(),
            node_store=store,
            logger=logger,
            max_attempts=1,
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        result = session.run()

        assert result.failed == ("a",)
        dropped = [
            e for e in logger.read_log() if e.event_type is EventType.SOURCE_DROPPED
        ]
        assert dropped, "an out-of-scope returned id must never be dropped silently"
        payload = dropped[0].payload
        assert payload["node_id"] == "other.py::function::x"
        assert payload["granted"] == ["m.py::function::a"]
        assert payload["source_length"] == len("def x():\n    return 1\n")

    def test_failure_reason_names_the_out_of_grant_ids(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        session = _session(
            tmp_path, runner=_StrayRunner(), node_store=store, max_attempts=1
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        reason = session.run().failure_reasons["a"]
        assert "none within its grant" in reason
        assert "other.py::function::x" in reason
        assert "m.py::function::a" in reason

    def test_failure_reason_distinguishes_an_empty_success(
        self, tmp_path: Path
    ) -> None:
        # The other candidate cause of the same operator-visible symptom: the
        # agent claimed success and returned nothing at all.
        store = _store(tmp_path)

        class NoOpRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                return TaskResult(task_id=task.task_id, success=True)

        session = _session(
            tmp_path, runner=NoOpRunner(), node_store=store, max_attempts=1
        )
        session.initialize()
        session.install_plan([_task("create", ["editor/motions.py"])])
        reason = session.run().failure_reasons["create"]
        assert "no sources" in reason
        assert "editor/motions.py" in reason

    def test_agent_result_event_records_the_wire_result(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        logger = SessionLogger(tmp_path / "session.log")
        new_source = "def a():\n    return 99\n"
        session = _session(
            tmp_path,
            runner=WireRunner({"m.py::function::a": new_source}),
            node_store=store,
            logger=logger,
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        assert session.run().ok

        (event,) = [
            e for e in logger.read_log() if e.event_type is EventType.AGENT_RESULT
        ]
        assert event.payload["task_id"] == "a"
        assert event.payload["attempt"] == 1
        assert event.payload["success"] is True
        assert event.payload["granted"] == ["m.py::function::a"]
        assert event.payload["returned_nodes"] == ["m.py::function::a"]
        assert event.payload["source_lengths"] == {
            "m.py::function::a": len(new_source)
        }


class TestNodeGranularityContract:
    """11.3d: symbol ids returned under a whole-file grant are folded, not lost."""

    def test_symbol_ids_fold_into_a_whole_file_grant(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        class FragmentRunner:
            """Returns symbol-level ids for a bare-path (whole-file) grant."""

            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                sources = {
                    NodeId("editor/motions.py::function::move_word"): (
                        "def move_word(buf):\n    return buf\n"
                    ),
                    NodeId("editor/motions.py::function::move_line"): (
                        "def move_line(buf):\n    return buf\n"
                    ),
                }
                return TaskResult(
                    task_id=task.task_id,
                    success=True,
                    modified_nodes=list(sources),
                    new_sources=sources,
                )

        session = _session(tmp_path, runner=FragmentRunner(), node_store=store)
        session.initialize()
        session.install_plan([_task("motions", ["editor/motions.py"])])
        result = session.run()

        assert result.ok, result.failure_reasons
        written = (tmp_path / "editor" / "motions.py").read_text()
        assert "def move_word" in written
        assert "def move_line" in written

    def test_source_for_an_ungranted_file_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        # Folding must not become a licence to write files the task never held a
        # lock on: only the granted file's own symbols are absorbed.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        session = _session(
            tmp_path, runner=_StrayRunner(), node_store=store, max_attempts=1
        )
        session.initialize()
        session.install_plan([_task("a", ["m.py::function::a"])])
        assert session.run().failed == ("a",)
        assert not (tmp_path / "other.py").exists()


# --- wave 13: dependency context ---------------------------------------------


class GreenfieldRunner:
    """An agent that returns whole-file source for brand-new files.

    ``sources`` maps a task id to ``{node_id: source}``. Everything it writes is
    new, which is the shape that starved the enrichment layers: for a task whose
    targets do not exist yet, layers 1-4 have nothing to find.
    """

    def __init__(self, sources: dict[str, dict[str, str]]) -> None:
        self._sources = sources
        self.assigned: list[TaskBundle] = []

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        self.assigned.append(task)
        new = {
            NodeId(node_id): source
            for node_id, source in self._sources.get(task.task_id, {}).items()
        }
        return TaskResult(
            task_id=task.task_id,
            success=bool(new),
            modified_nodes=list(new),
            new_sources=new,
        )


_CORE_SRC = (
    "GREETING = 'hi'\n\n\n"
    "def build(name, size):\n"
    "    return f'{name}:{size}'\n"
)
_RENDER_SRC = (
    "from core import build\n\n\n"
    "def render(name):\n"
    "    return build(name, 1)\n"
)


def _greenfield_plan() -> list[SubTask]:
    """Chain core -> render -> tests, every target a file that does not exist."""
    return [
        _task("core", ["core.py"]),
        _task("render", ["render.py"], deps=["core"]),
        _task("tests", ["test_all.py"], deps=["core", "render"]),
    ]


def _greenfield_sources() -> dict[str, dict[str, str]]:
    return {
        "core": {"core.py": _CORE_SRC},
        "render": {"render.py": _RENDER_SRC},
        "tests": {
            "test_all.py": (
                "from render import render\n\n\n"
                "def test_render():\n"
                "    assert render('a')\n"
            )
        },
    }


def _bundle_for(runner: GreenfieldRunner, task_id: str) -> TaskBundle:
    return next(b for b in runner.assigned if b.task_id == task_id)


class TestDependencyContext:
    """A task must arrive with the output of the tasks it depends on."""

    def _run_greenfield(
        self, tmp_path: Path, *, config: MakConfig | None = None
    ) -> tuple[GreenfieldRunner, Session, SessionLogger]:
        store = _store(tmp_path)
        runner = GreenfieldRunner(_greenfield_sources())
        logger = SessionLogger(tmp_path / "session.log")
        session = _session(
            tmp_path,
            runner=runner,
            node_store=store,
            logger=logger,
            config=config,
        )
        session.initialize()
        session.install_plan(_greenfield_plan())
        assert session.run().ok
        return runner, session, logger

    def test_dependency_output_reaches_the_dependent_task(
        self, tmp_path: Path
    ) -> None:
        # render depends on core and lists no context nodes: without layer 5 its
        # bundle is empty and it has to invent build()'s signature.
        runner, _session_obj, _logger = self._run_greenfield(tmp_path)
        ctx = _bundle_for(runner, "render").context
        assert "read_source:core.py" in ctx
        assert "def build(name, size)" in ctx["read_source:core.py"]

    def test_only_direct_dependencies_are_carried(self, tmp_path: Path) -> None:
        # tests depends on both core and render directly, so it gets both — but
        # the layer never walks past a direct edge.
        runner, _session_obj, _logger = self._run_greenfield(tmp_path)
        ctx = _bundle_for(runner, "tests").context
        assert "read_source:core.py" in ctx
        assert "read_source:render.py" in ctx

    def test_no_task_is_dispatched_context_empty(self, tmp_path: Path) -> None:
        runner, _session_obj, logger = self._run_greenfield(tmp_path)
        dispatched = [
            e for e in logger.read_log()
            if e.event_type is EventType.TASK_DISPATCHED
        ]
        assert {str(e.payload["task_id"]) for e in dispatched} == {
            "core", "render", "tests"
        }
        # 'core' has no dependencies and nothing exists yet, so an empty bundle
        # is legitimate there; every task that *has* an edge must carry context.
        for event in dispatched:
            if event.payload["depends_on"]:
                assert event.payload["starved"] is False
                assert int(str(event.payload["context_bytes"])) > 0

    def test_dispatch_event_counts_what_was_sent(self, tmp_path: Path) -> None:
        runner, _session_obj, logger = self._run_greenfield(tmp_path)
        event = next(
            e for e in logger.read_log()
            if e.event_type is EventType.TASK_DISPATCHED
            and e.payload["task_id"] == "render"
        )
        assert event.payload["read_sources"] == 1
        assert event.payload["attempt"] == 1
        assert event.payload["targets"] == ["render.py"]

    def test_metrics_report_context_volume(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        runner = GreenfieldRunner(_greenfield_sources())
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan(_greenfield_plan())
        metrics = session.run().metrics
        assert metrics["dispatches"] == 3.0
        assert metrics["context_bytes_total"] > 0
        assert metrics["starved_dispatches"] == 0.0

    def test_budget_degrades_to_an_api_digest(self, tmp_path: Path) -> None:
        # Past the byte budget the dependency's *contract* is sent instead of its
        # body — informed cheaply, never blind.
        config = MakConfig(
            session=SessionConfig(
                work_dir=str(tmp_path),
                mak_dir=str(tmp_path / ".mak"),
                dependency_context_bytes=1,
            ),
            git=GitConfig(auto_commit=False, auto_push=False),
            node_store=NodeStoreConfig(),
        )
        runner, _session_obj, _logger = self._run_greenfield(
            tmp_path, config=config
        )
        ctx = _bundle_for(runner, "render").context
        assert "read_source:core.py" not in ctx
        digest = ctx["read_api:core.py"]
        assert "def build(name, size):" in digest
        assert "return f'{name}:{size}'" not in digest

    def test_zero_context_dispatch_fails_as_a_kernel_defect(
        self, tmp_path: Path
    ) -> None:
        # Disabling the layer leaves a dependent greenfield task with literally
        # nothing. That is the kernel's defect, and it must not reach a model.
        config = MakConfig(
            session=SessionConfig(
                work_dir=str(tmp_path),
                mak_dir=str(tmp_path / ".mak"),
                dependency_context_bytes=0,
            ),
            git=GitConfig(auto_commit=False, auto_push=False),
            node_store=NodeStoreConfig(),
        )
        store = _store(tmp_path)
        runner = GreenfieldRunner(_greenfield_sources())
        session = _session(
            tmp_path, runner=runner, node_store=store, config=config
        )
        session.initialize()
        session.install_plan(_greenfield_plan())
        result = session.run()

        assert "render" in result.failed
        assert "kernel defect" in result.failure_reasons["render"]
        # The bundle was never handed to an agent.
        assert not any(b.task_id == "render" for b in runner.assigned)
        assert result.metrics["starved_dispatches"] >= 1.0

    def test_whole_file_target_still_gets_cross_file_callers(
        self, tmp_path: Path
    ) -> None:
        # Layer 4 derives its search symbols from target ids; a whole-file target
        # is a bare path with no name segment, which used to disable the layer.
        (tmp_path / "fruit.py").write_text("def apple():\n    return 1\n")
        (tmp_path / "animal.py").write_text(
            "from fruit import apple\n\n\ndef dog():\n    return apple()\n"
        )
        store = _store(tmp_path)
        runner = GreenfieldRunner(
            {"whole": {"fruit.py": "def apple():\n    return 2\n"}}
        )
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("whole", ["fruit.py"])])
        session.run()
        ctx = _bundle_for(runner, "whole").context
        assert any(
            k.startswith("read_source:animal.py") for k in ctx
        ), "the caller of the targeted file must still be enriched"


class TestCrossModuleDefects:
    """Modules created in one wave must agree with each other."""

    def _run_pair(self, tmp_path: Path, beta_src: str) -> Session:
        store = _store(tmp_path)
        runner = GreenfieldRunner({
            "alpha": {"alpha.py": "def f(a, b):\n    return a + b\n"},
            "beta": {"beta.py": beta_src},
        })
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("alpha", ["alpha.py"]), _task("beta", ["beta.py"])])
        assert session.run().ok
        return session

    def test_wrong_arity_between_new_modules_is_reported(
        self, tmp_path: Path
    ) -> None:
        session = self._run_pair(
            tmp_path, "from alpha import f\n\n\ndef g():\n    return f(1)\n"
        )
        defects = session.detect_cross_module_defects()
        assert [d.kind for d in defects] == ["signature_mismatch"]
        assert "missing required argument 'b'" in defects[0].detail

    def test_importing_a_name_the_module_lacks_is_reported(
        self, tmp_path: Path
    ) -> None:
        session = self._run_pair(
            tmp_path, "from alpha import missing\n\n\ndef g():\n    return 1\n"
        )
        defects = session.detect_cross_module_defects()
        assert [d.kind for d in defects] == ["unresolved_import"]
        assert "does not define it" in defects[0].detail

    def test_agreeing_modules_report_clean(self, tmp_path: Path) -> None:
        session = self._run_pair(
            tmp_path, "from alpha import f\n\n\ndef g():\n    return f(1, 2)\n"
        )
        assert session.detect_cross_module_defects() == []
        assert session.detect_cascade_tasks() == []

    def test_defects_become_cascade_fix_tasks(self, tmp_path: Path) -> None:
        session = self._run_pair(
            tmp_path, "from alpha import f\n\n\ndef g():\n    return f(1)\n"
        )
        tasks = session.detect_cascade_tasks()
        assert [t.task_id for t in tasks] == ["api_fix_beta_py"]
        assert tasks[0].target_nodes == [NodeId("beta.py")]
        assert NodeId("alpha.py") in tasks[0].context_nodes


class TestPhantomContextIsLockable:
    """A context node no one has written yet is a legal read-lock target.

    This is the objection that justified dropping such nodes in validation — the
    lock table is keyed by id and never consults the node store, and the ordering
    edge validation now adds means the node is committed before the reader runs.
    """

    def test_context_created_by_a_sibling_task_runs_end_to_end(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        runner = GreenfieldRunner({
            "build": {"home.py": "def banner(width, height):\n    return ''\n"},
            "check": {"test_home.py": "def test_banner():\n    assert True\n"},
        })
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([
            _task("build", ["home.py"]),
            _task("check", ["test_home.py"], context=["home.py"]),
        ])
        result = session.run()

        assert result.ok, result.failure_reasons
        ctx = _bundle_for(runner, "check").context
        assert "def banner(width, height)" in ctx["read_source:home.py"]


# --- wave 16: context budget -------------------------------------------------


_HOMEART = (
    "__all__ = ['pick_banner', 'Menu']\n"
    "DEFAULT_WIDTH = 80\n\n\n"
    "def pick_banner(width, height):\n"
    "    return (str(width), str(height))\n\n\n"
    "class Menu:\n"
    "    def render_menu(self):\n"
    "        return 'menu'\n"
)
# A module that shares nothing with homeart except the fact that it, like most
# well-formed modules, declares __all__.
_UNRELATED = (
    "__all__ = ['zebra']\n"
    "DEFAULT_WIDTH = 1\n\n\n"
    "def zebra():\n"
    "    return 'stripes'\n"
)


def _context_files(context: dict[str, str], prefix: str = "read_source:") -> set[str]:
    """File paths carried under ``prefix``, ignoring the fragment part of the id."""
    return {
        k[len(prefix):].split("::", 1)[0]
        for k in context
        if k.startswith(prefix)
    }


class TestCrossFileSymbolQuality:
    """A whole-file target contributes node names, not every name it binds."""

    def _run_whole_file(
        self, tmp_path: Path, *, config: MakConfig | None = None
    ) -> tuple[GreenfieldRunner, SessionLogger]:
        (tmp_path / "homeart.py").write_text(_HOMEART)
        (tmp_path / "caller.py").write_text(
            "from homeart import pick_banner\n\n\n"
            "def draw():\n"
            "    return pick_banner(80, 24)\n"
        )
        (tmp_path / "menu_user.py").write_text(
            "from homeart import Menu\n\n\n"
            "def show():\n"
            "    return Menu().render_menu()\n"
        )
        for i in range(4):
            (tmp_path / f"unrelated{i}.py").write_text(_UNRELATED)
        store = _store(tmp_path)
        runner = GreenfieldRunner(
            {"whole": {"homeart.py": _HOMEART.replace("80", "100")}}
        )
        logger = SessionLogger(tmp_path / "session.log")
        session = _session(
            tmp_path, runner=runner, node_store=store, logger=logger, config=config
        )
        session.initialize()
        session.install_plan([_task("whole", ["homeart.py"])])
        session.run()
        return runner, logger

    def test_all_dunder_does_not_drag_in_every_module(self, tmp_path: Path) -> None:
        # __all__ is not a node name. Treating it as one pulled in every module
        # that declares one — 88% of the largest bundle in a real run.
        runner, _logger = self._run_whole_file(tmp_path)
        files = _context_files(_bundle_for(runner, "whole").context)
        assert not any(f.startswith("unrelated") for f in files), files

    def test_module_constants_are_not_symbols(self, tmp_path: Path) -> None:
        # The unrelated modules also share DEFAULT_WIDTH; neither name counts.
        runner, _logger = self._run_whole_file(tmp_path)
        joined = " ".join(_bundle_for(runner, "whole").context)
        assert "unrelated" not in joined

    def test_real_function_caller_still_arrives(self, tmp_path: Path) -> None:
        runner, _logger = self._run_whole_file(tmp_path)
        assert "caller.py" in _context_files(
            _bundle_for(runner, "whole").context
        )

    def test_method_caller_still_arrives(self, tmp_path: Path) -> None:
        # Parity with a symbol-level target means methods count too: a fragmented
        # file yields its `::method::` ids here, so the AST path must agree.
        runner, _logger = self._run_whole_file(tmp_path)
        assert "menu_user.py" in _context_files(
            _bundle_for(runner, "whole").context
        )

    def test_short_symbols_are_not_evidence(self, tmp_path: Path) -> None:
        # `run` matched six unrelated files in the observed run.
        (tmp_path / "entry.py").write_text("def run():\n    return 1\n")
        (tmp_path / "elsewhere.py").write_text(
            "def other():\n    run = 1\n    return run\n"
        )
        store = _store(tmp_path)
        runner = GreenfieldRunner({"e": {"entry.py": "def run():\n    return 2\n"}})
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("e", ["entry.py"])])
        session.run()
        assert "elsewhere.py" not in _context_files(
            _bundle_for(runner, "e").context
        )

    def test_over_broad_symbol_is_discarded(self, tmp_path: Path) -> None:
        # A symbol in more nodes than _MAX_SYMBOL_MATCHES says nothing about which
        # of them is related, so it brings none of them in.
        (tmp_path / "widget.py").write_text(
            "def handle_event(e):\n    return e\n"
        )
        for i in range(10):
            (tmp_path / f"user{i}.py").write_text(
                f"def use{i}():\n    return handle_event({i})\n"
            )
        store = _store(tmp_path)
        runner = GreenfieldRunner(
            {"w": {"widget.py": "def handle_event(e):\n    return None\n"}}
        )
        session = _session(tmp_path, runner=runner, node_store=store)
        session.initialize()
        session.install_plan([_task("w", ["widget.py"])])
        session.run()
        files = _context_files(_bundle_for(runner, "w").context)
        assert not any(f.startswith("user") for f in files), files


class TestCrossFileBudget:
    """Layer 4 has a ceiling, and says when it hit one."""

    def _config(self, tmp_path: Path, budget: int) -> MakConfig:
        return MakConfig(
            session=SessionConfig(
                work_dir=str(tmp_path),
                mak_dir=str(tmp_path / ".mak"),
                cross_file_context_bytes=budget,
            ),
            git=GitConfig(auto_commit=False, auto_push=False),
            node_store=NodeStoreConfig(),
        )

    def _run(
        self, tmp_path: Path, budget: int
    ) -> tuple[GreenfieldRunner, SessionLogger]:
        (tmp_path / "widget.py").write_text("def handle_event(e):\n    return e\n")
        for i in range(3):
            (tmp_path / f"user{i}.py").write_text(
                f"def use{i}():\n    return handle_event({i})  # padding padding\n"
            )
        store = _store(tmp_path)
        runner = GreenfieldRunner(
            {"w": {"widget.py": "def handle_event(e):\n    return None\n"}}
        )
        logger = SessionLogger(tmp_path / "session.log")
        session = _session(
            tmp_path,
            runner=runner,
            node_store=store,
            logger=logger,
            config=self._config(tmp_path, budget),
        )
        session.initialize()
        session.install_plan([_task("w", ["widget.py"])])
        session.run()
        return runner, logger

    def test_unbounded_budget_carries_every_caller(self, tmp_path: Path) -> None:
        runner, _logger = self._run(tmp_path, -1)
        files = _context_files(_bundle_for(runner, "w").context)
        assert len([f for f in files if f.startswith("user")]) == 3

    def test_budget_truncates_and_reports_the_drop(self, tmp_path: Path) -> None:
        runner, logger = self._run(tmp_path, 60)
        files = _context_files(_bundle_for(runner, "w").context)
        carried = [f for f in files if f.startswith("user")]
        assert 0 < len(carried) < 3, carried
        event = next(
            e for e in logger.read_log()
            if e.event_type is EventType.TASK_DISPATCHED
        )
        assert int(str(event.payload["cross_file_dropped"])) > 0

    def test_zero_budget_disables_the_layer(self, tmp_path: Path) -> None:
        runner, _logger = self._run(tmp_path, 0)
        files = _context_files(_bundle_for(runner, "w").context)
        assert not any(f.startswith("user") for f in files)


class TestDispatchLayerAttribution:
    """Which layer put a node in the bundle is readable from the log alone."""

    def test_layers_name_their_nodes(self, tmp_path: Path) -> None:
        (tmp_path / "core.py").write_text("def build(name):\n    return name\n")
        (tmp_path / "caller.py").write_text(
            "from core import build\n\n\ndef go():\n    return build('x')\n"
        )
        store = _store(tmp_path)
        runner = GreenfieldRunner(
            {"c": {"core.py::function::build": "def build(name):\n    return 1\n"}}
        )
        logger = SessionLogger(tmp_path / "session.log")
        session = _session(
            tmp_path, runner=runner, node_store=store, logger=logger
        )
        session.initialize()
        session.install_plan([_task("c", ["core.py::function::build"])])
        session.run()

        event = next(
            e for e in logger.read_log()
            if e.event_type is EventType.TASK_DISPATCHED
        )
        layers = event.payload["layers"]
        assert isinstance(layers, dict)
        assert set(layers) == {
            "write_targets", "planner_context", "same_file",
            "cross_file", "dependency_output",
        }
        assert layers["write_targets"]["nodes"] == ["core.py::function::build"]
        assert any(
            n.startswith("caller.py") for n in layers["cross_file"]["nodes"]
        )
        assert layers["cross_file"]["bytes"] > 0
        assert layers["dependency_output"]["count"] == 0


class TestSchemaSlipRetry:
    """A retry after a shape error restates the shape (not "try again")."""

    def test_the_retry_note_names_the_required_schema(self, tmp_path: Path) -> None:
        # A real run returned `modified_fragments` as a string three times: the
        # generic note said the answer was unusable but never what shape was
        # wanted, so ~18k output tokens bought three identical rejections and
        # twelve dependent tasks were stranded behind the failure.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        notes: list[str | None] = []

        class SchemaSlipRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                notes.append(task.retry_note)
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error=(
                        "'modified_fragments' must be an array of "
                        "{node_id, new_source} objects, got string: 'def a(): ...'"
                    ),
                    error_kind="protocol",
                )

        session = _session(
            tmp_path, runner=SchemaSlipRunner(), node_store=store, max_attempts=3
        )
        session.initialize()
        session.install_plan([_task("edit", ["m.py"])])
        session.run()

        assert notes[0] is None
        assert notes[1] is not None
        assert "modified_fragments" in notes[1]
        assert "JSON array of objects" in notes[1]
        assert "not as a string" in notes[1]

    def test_a_truncation_still_gets_the_compaction_note(
        self, tmp_path: Path
    ) -> None:
        # The shape branch must not swallow the truncation case: a cut reply
        # needs a *smaller* answer, not a restated schema.
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        notes: list[str | None] = []

        class TruncatedProtocolRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                notes.append(task.retry_note)
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error="payload was cut off before 'success' arrived",
                    stop_reason="max_tokens",
                    error_kind="protocol",
                )

        session = _session(
            tmp_path,
            runner=TruncatedProtocolRunner(),
            node_store=store,
            max_attempts=3,
        )
        session.initialize()
        session.install_plan([_task("edit", ["m.py"])])
        session.run()
        assert notes[1] is not None
        assert "cut off" in notes[1]

    def test_an_ordinary_failure_keeps_the_generic_note(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 0\n")
        store = _store(tmp_path)
        notes: list[str | None] = []

        class ApiFailureRunner:
            def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
                notes.append(task.retry_note)
                return TaskResult(
                    task_id=task.task_id,
                    success=False,
                    error="api call failed: connection reset",
                    error_kind="api",
                )

        session = _session(
            tmp_path, runner=ApiFailureRunner(), node_store=store, max_attempts=2
        )
        session.initialize()
        session.install_plan([_task("edit", ["m.py"])])
        session.run()
        assert notes[1] is not None
        assert "modified_fragments" not in notes[1]
        assert "produced nothing usable" in notes[1]
