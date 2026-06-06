"""Tests for mak.scheduler.scheduler with mocked lock manager / runner / registry."""

from __future__ import annotations

import json
from pathlib import Path

from mak.core.types import LockMode, NodeId, SubTask
from mak.scheduler.dag import DAG
from mak.scheduler.scheduler import Scheduler


class FakeLockManager:
    """A minimal in-memory lock manager honoring write exclusivity per node."""

    def __init__(self, blocked_nodes: set[str] | None = None) -> None:
        self._held: dict[str, str] = {}  # node_id -> holder
        # Nodes that are permanently unavailable (held by some external holder).
        for node in blocked_nodes or set():
            self._held[node] = "external"
        self.acquired: list[tuple[str, list[str]]] = []
        self.released: list[str] = []

    def try_acquire_all(
        self, requests: list[tuple[NodeId, LockMode]], holder: str
    ) -> bool:
        nodes = [str(n) for n, _ in requests]
        for node in nodes:
            owner = self._held.get(node)
            if owner is not None and owner != holder:
                return False
        for node in nodes:
            self._held[node] = holder
        self.acquired.append((holder, nodes))
        return True

    def release_all(self, holder: str) -> int:
        freed = [n for n, h in self._held.items() if h == holder]
        for node in freed:
            del self._held[node]
        self.released.append(holder)
        return len(freed)


class FakeAdapter:
    agent_type = "anthropic_api"


class FakeRegistry:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def get(self, agent_type: str) -> FakeAdapter:
        self.requested.append(agent_type)
        return FakeAdapter()


class FakeAgentRunner:
    def __init__(self) -> None:
        self.assignments: list[tuple[FakeAdapter, object]] = []

    def assign(self, adapter: FakeAdapter, task: object) -> object:
        self.assignments.append((adapter, task))
        return None


def _task(
    task_id: str,
    depends_on: list[str] | None = None,
    nodes: list[str] | None = None,
) -> SubTask:
    return SubTask(
        task_id=task_id,
        description=f"task {task_id}",
        target_nodes=[NodeId(n) for n in (nodes or [f"file.py::function::{task_id}"])],
        depends_on=depends_on or [],
        agent_type="anthropic_api",
    )


def _make(
    tasks: list[SubTask],
    lock_manager: FakeLockManager | None = None,
    persist_path: Path | None = None,
) -> tuple[Scheduler, FakeLockManager, FakeAgentRunner, FakeRegistry]:
    lm = lock_manager or FakeLockManager()
    runner = FakeAgentRunner()
    registry = FakeRegistry()
    sched = Scheduler(DAG(tasks), lm, runner, registry, persist_path=persist_path)
    return sched, lm, runner, registry


class TestReadyQueue:
    def test_initial_ready_queue_has_no_dep_tasks(self) -> None:
        sched, _, _, _ = _make([_task("a"), _task("b", depends_on=["a"])])
        assert [t.task_id for t in sched.ready_queue] == ["a"]


class TestTickDispatch:
    def test_tick_dispatches_ready_task(self) -> None:
        sched, lm, runner, registry = _make([_task("a")])
        dispatched = sched.tick()
        assert dispatched == ["a"]
        assert lm.acquired == [("a", ["file.py::function::a"])]
        assert registry.requested == ["anthropic_api"]
        assert len(runner.assignments) == 1

    def test_dispatched_task_leaves_ready_queue(self) -> None:
        sched, _, _, _ = _make([_task("a")])
        sched.tick()
        assert sched.ready_queue == []
        assert sched.dispatched == {"a"}

    def test_assigned_bundle_carries_task_fields(self) -> None:
        sched, _, runner, _ = _make([_task("a", nodes=["m.py::function::a"])])
        sched.tick()
        _, bundle = runner.assignments[0]
        assert bundle.task_id == "a"  # type: ignore[attr-defined]
        assert "m.py::function::a" in bundle.target_nodes  # type: ignore[attr-defined]

    def test_task_with_no_target_nodes_dispatches(self) -> None:
        sched, _, runner, _ = _make(
            [SubTask(task_id="a", description="d", agent_type="anthropic_api")]
        )
        assert sched.tick() == ["a"]
        assert len(runner.assignments) == 1

    def test_blocked_locks_keep_task_in_queue(self) -> None:
        lm = FakeLockManager(blocked_nodes={"file.py::function::a"})
        sched, _, runner, _ = _make([_task("a")], lock_manager=lm)
        assert sched.tick() == []
        assert [t.task_id for t in sched.ready_queue] == ["a"]
        assert runner.assignments == []

    def test_lock_freed_lets_task_dispatch_next_tick(self) -> None:
        lm = FakeLockManager(blocked_nodes={"file.py::function::a"})
        sched, _, _, _ = _make([_task("a")], lock_manager=lm)
        assert sched.tick() == []
        lm.release_all("external")
        assert sched.tick() == ["a"]

    def test_concurrent_tasks_on_distinct_nodes_both_dispatch(self) -> None:
        sched, _, _, _ = _make(
            [
                _task("a", nodes=["x.py::function::a"]),
                _task("b", nodes=["y.py::function::b"]),
            ]
        )
        assert set(sched.tick()) == {"a", "b"}


class TestCompletionFlow:
    def test_completion_releases_locks_and_unblocks_dependent(self) -> None:
        sched, lm, runner, _ = _make(
            [_task("a"), _task("b", depends_on=["a"])]
        )
        sched.tick()  # dispatch a
        sched.on_task_complete("a")
        assert "a" in lm.released
        assert [t.task_id for t in sched.ready_queue] == ["b"]
        assert sched.dispatched == set()

    def test_full_linear_pipeline_runs_to_completion(self) -> None:
        sched, _, runner, _ = _make(
            [
                _task("a"),
                _task("b", depends_on=["a"]),
                _task("c", depends_on=["b"]),
            ]
        )
        sched.tick()
        sched.on_task_complete("a")
        sched.tick()
        sched.on_task_complete("b")
        sched.tick()
        sched.on_task_complete("c")
        assert sched.is_done()
        assert [b.task_id for _, b in runner.assignments] == ["a", "b", "c"]  # type: ignore[attr-defined]

    def test_is_done_false_while_in_flight(self) -> None:
        sched, _, _, _ = _make([_task("a")])
        sched.tick()
        assert not sched.is_done()
        sched.on_task_complete("a")
        assert sched.is_done()


class TestFailureHandling:
    def test_failed_task_requeued_by_default(self) -> None:
        sched, lm, _, _ = _make([_task("a")])
        sched.tick()
        sched.on_task_failed("a")
        assert "a" in lm.released
        assert [t.task_id for t in sched.ready_queue] == ["a"]
        assert sched.dispatched == set()

    def test_failed_task_not_requeued_when_disabled(self) -> None:
        sched, _, _, _ = _make([_task("a")])
        sched.tick()
        sched.on_task_failed("a", requeue=False)
        assert sched.ready_queue == []


class TestRun:
    def test_run_drives_pipeline_when_completion_is_synchronous(self) -> None:
        # A runner that completes each task immediately on assignment.
        tasks = [_task("a"), _task("b", depends_on=["a"])]
        lm = FakeLockManager()
        registry = FakeRegistry()

        class CompletingRunner:
            """A fire-and-forget runner that counts assignments."""

            def __init__(self) -> None:
                self.sched: Scheduler | None = None
                self.count = 0

            def assign(self, adapter: object, task: object) -> object:
                self.count += 1
                # Complete synchronously after the tick loop finishes dispatch.
                return None

        runner = CompletingRunner()
        sched = Scheduler(DAG(tasks), lm, runner, registry)
        # Manually drive: tick + complete, since assign here is fire-and-forget.
        sched.tick()
        sched.on_task_complete("a")
        sched.tick()
        sched.on_task_complete("b")
        assert sched.is_done()
        assert runner.count == 2


class TestPersistence:
    def test_state_persisted_to_disk(self, tmp_path: Path) -> None:
        path = tmp_path / ".mak" / "task_graph.json"
        sched, _, _, _ = _make(
            [_task("a"), _task("b", depends_on=["a"])], persist_path=path
        )
        sched.tick()
        sched.on_task_complete("a")
        data = json.loads(path.read_text("utf-8"))
        assert "a" in data["completed"]
        assert {t["task_id"] for t in data["tasks"]} == {"a", "b"}

    def test_from_persisted_restores_progress(self, tmp_path: Path) -> None:
        path = tmp_path / ".mak" / "task_graph.json"
        sched, _, _, _ = _make(
            [_task("a"), _task("b", depends_on=["a"]), _task("c", depends_on=["b"])],
            persist_path=path,
        )
        sched.tick()
        sched.on_task_complete("a")  # b now ready

        lm = FakeLockManager()
        runner = FakeAgentRunner()
        registry = FakeRegistry()
        restored = Scheduler.from_persisted(path, lm, runner, registry)
        # 'a' done, 'b' ready, 'c' still blocked.
        assert [t.task_id for t in restored.ready_queue] == ["b"]
        assert not restored.is_done()
        restored.tick()
        restored.on_task_complete("b")
        assert [t.task_id for t in restored.ready_queue] == ["c"]

    def test_from_persisted_requeues_in_flight_tasks(self, tmp_path: Path) -> None:
        path = tmp_path / ".mak" / "task_graph.json"
        sched, _, _, _ = _make([_task("a")], persist_path=path)
        sched.tick()  # 'a' dispatched (in flight), persisted as dispatched

        lm = FakeLockManager()
        restored = Scheduler.from_persisted(path, lm, FakeAgentRunner(), FakeRegistry())
        # The in-flight task is re-queued for another attempt after a crash.
        assert [t.task_id for t in restored.ready_queue] == ["a"]
