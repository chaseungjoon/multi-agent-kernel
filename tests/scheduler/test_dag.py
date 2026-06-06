"""Tests for mak.scheduler.dag."""

from __future__ import annotations

import pytest

from mak.core.exceptions import SchedulingError
from mak.core.types import NodeId, SubTask
from mak.scheduler.dag import DAG


def _task(task_id: str, depends_on: list[str] | None = None) -> SubTask:
    return SubTask(
        task_id=task_id,
        description=f"task {task_id}",
        target_nodes=[NodeId(f"file.py::function::f_{task_id}")],
        depends_on=depends_on or [],
        agent_type="anthropic_api",
    )


class TestConstruction:
    def test_empty_dag(self) -> None:
        dag = DAG([])
        assert dag.topological_order() == []
        assert dag.all_complete() is True
        assert dag.newly_unblocked() == []

    def test_single_task_no_deps(self) -> None:
        dag = DAG([_task("a")])
        assert dag.topological_order() == ["a"]

    def test_duplicate_task_id_raises(self) -> None:
        with pytest.raises(SchedulingError, match="duplicate task id"):
            DAG([_task("a"), _task("a")])

    def test_unknown_dependency_raises(self) -> None:
        with pytest.raises(SchedulingError, match="unknown task 'ghost'"):
            DAG([_task("a", depends_on=["ghost"])])

    def test_self_dependency_raises(self) -> None:
        with pytest.raises(SchedulingError, match="depends on itself"):
            DAG([_task("a", depends_on=["a"])])

    def test_two_node_cycle_raises(self) -> None:
        with pytest.raises(SchedulingError, match="cycle"):
            DAG([_task("a", depends_on=["b"]), _task("b", depends_on=["a"])])

    def test_three_node_cycle_raises(self) -> None:
        with pytest.raises(SchedulingError, match="cycle"):
            DAG(
                [
                    _task("a", depends_on=["c"]),
                    _task("b", depends_on=["a"]),
                    _task("c", depends_on=["b"]),
                ]
            )


class TestTopologicalSort:
    def test_linear_chain(self) -> None:
        dag = DAG(
            [
                _task("c", depends_on=["b"]),
                _task("b", depends_on=["a"]),
                _task("a"),
            ]
        )
        assert dag.topological_order() == ["a", "b", "c"]

    def test_dependencies_precede_dependents(self) -> None:
        dag = DAG(
            [
                _task("a"),
                _task("b", depends_on=["a"]),
                _task("c", depends_on=["a"]),
                _task("d", depends_on=["b", "c"]),
            ]
        )
        order = dag.topological_order()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_order_is_deterministic(self) -> None:
        tasks = [_task("a"), _task("b"), _task("c", depends_on=["a", "b"])]
        assert DAG(tasks).topological_order() == DAG(tasks).topological_order()

    def test_duplicate_edges_ignored(self) -> None:
        # A dependency listed twice must not corrupt the indegree bookkeeping.
        dag = DAG([_task("a"), _task("b", depends_on=["a", "a"])])
        assert dag.topological_order() == ["a", "b"]


class TestExecutionState:
    def test_initial_ready_set_has_no_dep_tasks(self) -> None:
        dag = DAG(
            [
                _task("a"),
                _task("b"),
                _task("c", depends_on=["a"]),
            ]
        )
        ready = {t.task_id for t in dag.newly_unblocked()}
        assert ready == {"a", "b"}

    def test_newly_unblocked_returns_each_task_once(self) -> None:
        dag = DAG([_task("a"), _task("b", depends_on=["a"])])
        first = [t.task_id for t in dag.newly_unblocked()]
        assert first == ["a"]
        # Nothing newly unblocked until 'a' completes.
        assert dag.newly_unblocked() == []

    def test_completing_dependency_unblocks_dependent(self) -> None:
        dag = DAG([_task("a"), _task("b", depends_on=["a"])])
        dag.newly_unblocked()  # drains initial ready set ['a']
        dag.mark_complete("a")
        unblocked = [t.task_id for t in dag.newly_unblocked()]
        assert unblocked == ["b"]

    def test_multi_dependency_waits_for_all(self) -> None:
        dag = DAG(
            [
                _task("a"),
                _task("b"),
                _task("c", depends_on=["a", "b"]),
            ]
        )
        dag.newly_unblocked()
        dag.mark_complete("a")
        assert dag.newly_unblocked() == []  # 'b' not done yet
        dag.mark_complete("b")
        assert [t.task_id for t in dag.newly_unblocked()] == ["c"]

    def test_diamond_resolution(self) -> None:
        dag = DAG(
            [
                _task("a"),
                _task("b", depends_on=["a"]),
                _task("c", depends_on=["a"]),
                _task("d", depends_on=["b", "c"]),
            ]
        )
        dag.newly_unblocked()
        dag.mark_complete("a")
        assert {t.task_id for t in dag.newly_unblocked()} == {"b", "c"}
        dag.mark_complete("b")
        assert dag.newly_unblocked() == []
        dag.mark_complete("c")
        assert [t.task_id for t in dag.newly_unblocked()] == ["d"]

    def test_all_complete_tracks_progress(self) -> None:
        dag = DAG([_task("a"), _task("b", depends_on=["a"])])
        assert not dag.all_complete()
        dag.mark_complete("a")
        assert not dag.all_complete()
        dag.mark_complete("b")
        assert dag.all_complete()

    def test_remaining_excludes_completed(self) -> None:
        dag = DAG([_task("a"), _task("b", depends_on=["a"])])
        assert dag.remaining() == ["a", "b"]
        dag.mark_complete("a")
        assert dag.remaining() == ["b"]

    def test_mark_complete_unknown_raises(self) -> None:
        dag = DAG([_task("a")])
        with pytest.raises(SchedulingError, match="unknown task"):
            dag.mark_complete("ghost")

    def test_get_task_unknown_raises(self) -> None:
        dag = DAG([_task("a")])
        with pytest.raises(SchedulingError, match="unknown task"):
            dag.get_task("ghost")

    def test_mark_released_suppresses_emission(self) -> None:
        dag = DAG([_task("a"), _task("b")])
        dag.mark_released("a")
        assert [t.task_id for t in dag.newly_unblocked()] == ["b"]
