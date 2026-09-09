"""Tests for the shared post-wave cascade loop (Wave 16, step 5)."""

from __future__ import annotations

from typing import Any

from mak.cascade import run_cascade_waves
from mak.core.types import NodeId, SubTask


def _task(task_id: str) -> SubTask:
    return SubTask(
        task_id=task_id,
        description=task_id,
        target_nodes=[NodeId(f"{task_id}.py")],
    )


class FakeSession:
    """A session whose ``detect_cascade_tasks`` returns a scripted sequence."""

    def __init__(self, batches: list[list[SubTask]]) -> None:
        self._batches = list(batches)
        self.installed: list[list[SubTask]] = []
        self.runs = 0

    def detect_cascade_tasks(self) -> list[SubTask]:
        return self._batches.pop(0) if self._batches else []

    def install_plan(self, subtasks: list[SubTask]) -> None:
        self.installed.append(list(subtasks))

    def run(self, max_iterations: int = 1000) -> str:
        self.runs += 1
        return f"result-{self.runs}"


def _accept(tasks: list[SubTask]) -> list[SubTask] | None:
    return tasks


def _decline(tasks: list[SubTask]) -> list[SubTask] | None:
    return None


class TestRunCascadeWaves:
    def test_no_cascade_runs_nothing(self) -> None:
        session = FakeSession([[]])
        outcome = run_cascade_waves(session, _accept)  # type: ignore[arg-type]
        # An outcome, never None: "nothing was detected" is a result the
        # caller aggregates like any other, and it is clean.
        assert outcome.waves == ()
        assert outcome.clean
        assert not outcome.declined and not outcome.limit_reached
        assert session.runs == 0
        assert session.installed == []

    def test_a_detected_batch_is_installed_and_run(self) -> None:
        session = FakeSession([[_task("fix_a")], []])
        outcome = run_cascade_waves(session, _accept)  # type: ignore[arg-type]
        assert outcome.waves == ("result-1",)
        assert [t.task_id for t in session.installed[0]] == ["fix_a"]
        assert session.runs == 1

    def test_it_repeats_until_the_session_reports_clean(self) -> None:
        session = FakeSession([[_task("a")], [_task("b")], []])
        outcome = run_cascade_waves(session, _accept)  # type: ignore[arg-type]
        # Every wave is kept, not just the last: they are separate claims
        # about separate work.
        assert outcome.waves == ("result-1", "result-2")
        assert session.runs == 2

    def test_declining_stops_without_running(self) -> None:
        session = FakeSession([[_task("a")], []])
        outcome = run_cascade_waves(session, _decline)  # type: ignore[arg-type]
        assert session.runs == 0
        # A declined wave is a *structured* outcome now. It used to be
        # indistinguishable from "nothing was detected".
        assert outcome.declined
        assert not outcome.clean

    def test_the_approver_may_edit_the_plan(self) -> None:
        session = FakeSession([[_task("a"), _task("b")], []])
        run_cascade_waves(  # type: ignore[arg-type]
            session, lambda tasks: tasks[:1]
        )
        assert [t.task_id for t in session.installed[0]] == ["a"]

    def test_announce_sees_each_batch_before_approval(self) -> None:
        seen: list[list[str]] = []
        session = FakeSession([[_task("a")], [_task("b")], []])
        run_cascade_waves(  # type: ignore[arg-type]
            session,
            _accept,
            announce=lambda tasks: seen.append([t.task_id for t in tasks]),
        )
        assert seen == [["a"], ["b"]]

    def test_announce_is_not_called_when_there_is_nothing_to_report(self) -> None:
        calls: list[Any] = []
        session = FakeSession([[]])
        run_cascade_waves(  # type: ignore[arg-type]
            session, _accept, announce=calls.append
        )
        assert calls == []

    def test_max_waves_bounds_a_self_feeding_loop(self) -> None:
        # A fix-up wave that keeps producing fix-up work must not spin forever.
        session = FakeSession([[_task("a")] for _ in range(50)])
        outcome = run_cascade_waves(  # type: ignore[arg-type]
            session, _accept, max_waves=3
        )
        assert session.runs == 3
        # Reaching the ceiling with defects left is reported, not hidden
        # behind the last wave's successful result.
        assert outcome.limit_reached
        assert outcome.unresolved == ("a",)
        assert not outcome.clean
