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
        assert run_cascade_waves(session, _accept) is None  # type: ignore[arg-type]
        assert session.runs == 0
        assert session.installed == []

    def test_a_detected_batch_is_installed_and_run(self) -> None:
        session = FakeSession([[_task("fix_a")], []])
        result = run_cascade_waves(session, _accept)  # type: ignore[arg-type]
        assert result == "result-1"
        assert [t.task_id for t in session.installed[0]] == ["fix_a"]
        assert session.runs == 1

    def test_it_repeats_until_the_session_reports_clean(self) -> None:
        session = FakeSession([[_task("a")], [_task("b")], []])
        result = run_cascade_waves(session, _accept)  # type: ignore[arg-type]
        assert result == "result-2"
        assert session.runs == 2

    def test_declining_stops_without_running(self) -> None:
        session = FakeSession([[_task("a")], []])
        assert run_cascade_waves(session, _decline) is None  # type: ignore[arg-type]
        assert session.runs == 0

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
        run_cascade_waves(session, _accept, max_waves=3)  # type: ignore[arg-type]
        assert session.runs == 3
