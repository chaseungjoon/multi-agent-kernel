"""Tests for the shared post-wave cascade loop (Wave 16, step 5)."""

from __future__ import annotations

from typing import Any

from mak.cascade import run_cascade_waves
from mak.core.types import NodeId, RepairObligation, SubTask


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


class StatefulFakeSession(FakeSession):
    """A fake exposing the durable fingerprint API used by real sessions."""

    def __init__(self, batches: list[list[SubTask]], fingerprints: list[str]) -> None:
        super().__init__(batches)
        self._fingerprints = list(fingerprints)
        self.history: list[str] = []

    def cascade_state_fingerprint(self, tasks: list[SubTask]) -> str:
        return self._fingerprints.pop(0)

    def cascade_history(self) -> tuple[str, ...]:
        return tuple(self.history)

    def remember_cascade_state(self, fingerprint: str) -> None:
        self.history.append(fingerprint)


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
        session = FakeSession([[_task(f"a{i}")] for i in range(50)])
        outcome = run_cascade_waves(  # type: ignore[arg-type]
            session, _accept, max_waves=3
        )
        assert session.runs == 3
        # Reaching the ceiling with defects left is reported, not hidden
        # behind the last wave's successful result.
        assert outcome.limit_reached
        assert outcome.unresolved == ("a3",)
        assert not outcome.clean

    def test_identical_broken_state_stops_before_a_second_prompt(self) -> None:
        session = FakeSession([[_task("a")], [_task("a")]])
        approvals = 0

        def approve(tasks: list[SubTask]) -> list[SubTask]:
            nonlocal approvals
            approvals += 1
            return tasks

        outcome = run_cascade_waves(session, approve)  # type: ignore[arg-type]

        assert session.runs == 1
        assert approvals == 1
        assert outcome.stalled
        assert not outcome.oscillating
        assert outcome.unresolved == ("a",)

    def test_revisited_state_stops_an_a_b_a_oscillation(self) -> None:
        session = FakeSession([[_task("a")], [_task("b")], [_task("a")]])
        outcome = run_cascade_waves(session, _accept)  # type: ignore[arg-type]

        assert session.runs == 2
        assert outcome.oscillating
        assert not outcome.stalled
        assert outcome.unresolved == ("a",)

    def test_review_edit_cannot_erase_repair_obligation(self) -> None:
        obligation = RepairObligation(
            kind="unresolved_import",
            file="caller.py",
            defining_file="provider.py",
            detail="missing",
            exact_key="exact",
            family_key="family",
        )
        detected = SubTask(
            task_id="generated",
            description="generated",
            target_nodes=[NodeId("caller.py")],
            repair_obligations=(obligation,),
        )
        edited = SubTask(
            task_id="edited",
            description="edited",
            target_nodes=[NodeId("caller.py")],
        )
        session = FakeSession([[detected], []])

        run_cascade_waves(  # type: ignore[arg-type]
            session, lambda _tasks: [edited]
        )

        installed = session.installed[0][0]
        assert installed.task_id == "edited"
        assert installed.repair_obligations == (obligation,)

    def test_review_edit_cannot_drop_every_repair_target(self) -> None:
        obligation = RepairObligation(
            kind="unresolved_import",
            file="caller.py",
            defining_file="provider.py",
            detail="missing",
            exact_key="exact",
            family_key="family",
        )
        detected = SubTask(
            task_id="generated",
            description="generated",
            target_nodes=[NodeId("caller.py")],
            repair_obligations=(obligation,),
        )
        unrelated = SubTask(
            task_id="edited",
            description="edited",
            target_nodes=[NodeId("other.py")],
        )
        session = FakeSession([[detected]])

        outcome = run_cascade_waves(  # type: ignore[arg-type]
            session, lambda _tasks: [unrelated]
        )

        assert session.runs == 0
        assert session.installed == []
        assert outcome.unrepairable
        assert outcome.unresolved == ("generated",)

    def test_split_review_validates_after_both_repair_writers(self) -> None:
        obligation = RepairObligation(
            kind="unresolved_import",
            file="caller.py",
            defining_file="provider.py",
            detail="missing",
            exact_key="exact",
            family_key="family",
        )
        detected = SubTask(
            task_id="generated",
            description="generated",
            target_nodes=[NodeId("caller.py"), NodeId("provider.py")],
            repair_obligations=(obligation,),
        )
        caller = SubTask(
            task_id="caller",
            description="repair caller",
            target_nodes=[NodeId("caller.py")],
        )
        provider = SubTask(
            task_id="provider",
            description="repair provider",
            target_nodes=[NodeId("provider.py")],
        )
        session = FakeSession([[detected], []])

        run_cascade_waves(  # type: ignore[arg-type]
            session, lambda _tasks: [caller, provider]
        )

        installed_caller, installed_provider = session.installed[0]
        assert installed_caller.repair_obligations == ()
        assert installed_provider.repair_obligations == (obligation,)
        assert installed_provider.depends_on == ["caller"]

    def test_declined_state_is_not_persisted(self) -> None:
        session = StatefulFakeSession([[_task("a")]], ["state-a"])

        outcome = run_cascade_waves(session, _decline)  # type: ignore[arg-type]

        assert outcome.declined
        assert session.history == []

    def test_state_is_persisted_only_when_a_repair_runs(self) -> None:
        session = StatefulFakeSession([[_task("a")], [], []], ["state-a"])

        run_cascade_waves(session, _accept)  # type: ignore[arg-type]

        assert session.history == ["state-a"]
