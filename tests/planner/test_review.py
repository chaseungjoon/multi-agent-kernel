"""Tests for mak.planner.review (human-in-the-loop DAG review)."""

from __future__ import annotations

import json

import pytest

from mak.core.exceptions import PlanReviewAborted
from mak.core.types import NodeId, SubTask
from mak.planner.review import display_plan_for_review, render_plan


def _plan() -> list[SubTask]:
    return [
        SubTask(
            task_id="a",
            description="do A",
            target_nodes=[NodeId("m.py::function::a")],
            agent_type="anthropic_api",
        ),
        SubTask(
            task_id="b",
            description="do B",
            depends_on=["a"],
            agent_type="anthropic_api",
        ),
    ]


class ScriptedIO:
    """Captures printed output and replays scripted prompt answers."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = answers
        self.output: list[str] = []

    def prompt(self, message: str) -> str:
        self.output.append(message)
        if not self._answers:
            raise AssertionError("ScriptedIO ran out of answers")
        return self._answers.pop(0)

    def printer(self, message: str) -> None:
        self.output.append(message)

    @property
    def text(self) -> str:
        return "\n".join(self.output)


class TestRenderPlan:
    def test_renders_tasks_and_edges(self) -> None:
        text = render_plan(_plan())
        assert "[a] do A" in text
        assert "[b] do B" in text
        assert "a -> b" in text

    def test_empty_plan(self) -> None:
        assert "empty plan" in render_plan([])

    def test_no_edges_placeholder(self) -> None:
        independent = [SubTask(task_id="x", description="d")]
        assert "independent" in render_plan(independent)


class TestApprove:
    def test_approve_returns_plan_unchanged(self) -> None:
        io = ScriptedIO(["a"])
        result = display_plan_for_review(
            _plan(), prompt_fn=io.prompt, printer=io.printer
        )
        assert [t.task_id for t in result] == ["a", "b"]

    def test_empty_input_approves(self) -> None:
        io = ScriptedIO([""])
        result = display_plan_for_review(
            _plan(), prompt_fn=io.prompt, printer=io.printer
        )
        assert len(result) == 2

    def test_plan_is_printed(self) -> None:
        io = ScriptedIO(["approve"])
        display_plan_for_review(_plan(), prompt_fn=io.prompt, printer=io.printer)
        assert "do A" in io.text


class TestAbort:
    def test_abort_raises(self) -> None:
        io = ScriptedIO(["b"])
        with pytest.raises(PlanReviewAborted):
            display_plan_for_review(_plan(), prompt_fn=io.prompt, printer=io.printer)

    def test_abort_word(self) -> None:
        io = ScriptedIO(["abort"])
        with pytest.raises(PlanReviewAborted):
            display_plan_for_review(_plan(), prompt_fn=io.prompt, printer=io.printer)


class TestEdit:
    def test_edit_replaces_plan(self) -> None:
        new_plan = json.dumps(
            [{"task_id": "z", "description": "replacement", "agent_type": "openai_api"}]
        )
        io = ScriptedIO(["e", new_plan])
        result = display_plan_for_review(
            _plan(), prompt_fn=io.prompt, printer=io.printer
        )
        assert [t.task_id for t in result] == ["z"]

    def test_blank_edit_cancels_then_approve(self) -> None:
        # Blank edit input cancels the edit and returns to the menu, where the
        # user then approves the original plan.
        io = ScriptedIO(["e", "", "a"])
        result = display_plan_for_review(
            _plan(), prompt_fn=io.prompt, printer=io.printer
        )
        assert [t.task_id for t in result] == ["a", "b"]

    def test_invalid_edit_reprompts(self) -> None:
        # A malformed edit is rejected (printed) and the menu reappears; the user
        # then approves the original plan.
        io = ScriptedIO(["e", "{bad json", "a"])
        result = display_plan_for_review(
            _plan(), prompt_fn=io.prompt, printer=io.printer
        )
        assert [t.task_id for t in result] == ["a", "b"]
        assert "rejected" in io.text

    def test_unrecognized_choice_reprompts(self) -> None:
        io = ScriptedIO(["huh?", "a"])
        result = display_plan_for_review(
            _plan(), prompt_fn=io.prompt, printer=io.printer
        )
        assert len(result) == 2
        assert "Unrecognized choice" in io.text
