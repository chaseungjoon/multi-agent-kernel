"""Wave 7 in the interactive app: proposals are shown, summarized and declinable."""

from __future__ import annotations

import io
from typing import Any

import prompt_toolkit
import pytest
from cli.app import MakCli
from cli.core.state import CliState
from cli.runner import plan_in_thread
from cli.ui import show_plan
from rich.console import Console

from mak.core.types import NodeId, SubTask
from mak.planner.validation import PlanFinding
from mak.session import PlanProposal


def _tasks() -> list[SubTask]:
    return [
        SubTask(task_id="change", description="change f",
                target_nodes=[NodeId("lib.py::function::f")], changes_api=True),
        SubTask(task_id="mak.callers.1", description="update callers",
                target_nodes=[NodeId("a.py::function::a")],
                context_nodes=[NodeId("lib.py::function::f")],
                depends_on=["change"], changes_api=False),
    ]


def _render(**kwargs: Any) -> str:
    buffer = io.StringIO()
    show_plan(Console(file=buffer, highlight=False, width=200), _tasks(), **kwargs)
    return buffer.getvalue()


class TestShowPlan:
    def test_proposed_tasks_are_marked_and_summarized(self) -> None:
        findings = [
            PlanFinding("missing_caller", "change", "added: 'mak.callers.1' updates"),
            PlanFinding("undeclared_api_callers", "x", "2 graph callers ..."),
        ]
        text = _render(proposed=frozenset({"mak.callers.1"}), findings=findings)
        assert "mak.callers.1  proposed by MAK" in text
        assert "MAK added 1 caller-update task for 1 signature change" in text
        assert "1 advisory validation finding" in text

    def test_a_plain_plan_shows_no_summary(self) -> None:
        text = _render()
        assert "proposed by MAK" not in text
        assert "MAK added" not in text


class TestPlanInThread:
    def test_returns_the_whole_proposal(self) -> None:
        proposal = PlanProposal(subtasks=_tasks(), findings=[],
                                proposed_task_ids=frozenset({"mak.callers.1"}))

        class FakeSession:
            def propose_plan(self, task: str) -> PlanProposal:
                return proposal

        result, error = plan_in_thread(FakeSession(), "t")  # type: ignore[arg-type]
        assert error is None and result is proposal

    def test_returns_the_error(self) -> None:
        class FakeSession:
            def propose_plan(self, task: str) -> PlanProposal:
                raise RuntimeError("planner down")

        result, error = plan_in_thread(FakeSession(), "t")  # type: ignore[arg-type]
        assert result is None and isinstance(error, RuntimeError)


class TestConfirmProposal:
    def _cli(self, confirm: bool = True) -> MakCli:
        cli = MakCli.__new__(MakCli)
        cli.console = Console(file=io.StringIO(), highlight=False)
        cli.state = CliState()
        cli._confirm_plan = lambda: confirm  # type: ignore[method-assign]
        return cli

    @pytest.mark.parametrize(
        ("answer", "choice"),
        [("y", "all"), ("yes", "all"), ("o", "without"), ("", None), ("n", None)],
    )
    def test_with_proposals(
        self, monkeypatch: pytest.MonkeyPatch, answer: str, choice: str | None
    ) -> None:
        monkeypatch.setattr(prompt_toolkit, "prompt", lambda *a, **k: answer)
        assert self._cli()._confirm_proposal(True) == choice

    def test_without_proposals_it_is_the_plain_prompt(self) -> None:
        assert self._cli(confirm=True)._confirm_proposal(False) == "all"
        assert self._cli(confirm=False)._confirm_proposal(False) is None
