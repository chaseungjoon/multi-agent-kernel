"""Tests for mak.planner.review (human-in-the-loop DAG review)."""

from __future__ import annotations

import json

import pytest

from mak.core.exceptions import PlanReviewAborted
from mak.core.types import NodeId, RepairObligation, SubTask
from mak.planner.review import display_plan_for_review, render_plan
from mak.planner.validation import PlanFinding


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

    def test_findings_none_or_empty_identical_to_today(self) -> None:
        base = render_plan(_plan())
        assert render_plan(_plan(), None) == base
        assert render_plan(_plan(), []) == base
        assert "Plan validation:" not in base

    def test_findings_rendered_with_marks(self) -> None:
        findings = [
            PlanFinding("corrected_node", "a", "corrected 'x' -> 'y'", ("y",)),
            PlanFinding("missing_dep", "b", "added: 'a' -> 'b' (rewrites node)"),
            PlanFinding("unknown_node", "a", "target 'z' is not in the inventory",
                        ("z1", "z2")),
        ]
        text = render_plan(_plan(), findings)
        assert "Plan validation:" in text
        assert "✎ [a] corrected" in text  # applied change
        assert "✎ [b] added:" in text  # added edge
        assert "⚠ [a] target 'z'" in text  # advisory suggestion
        assert "candidates: z1, z2" in text

    def test_repair_postcondition_is_visible(self) -> None:
        obligation = RepairObligation(
            kind="unresolved_import",
            file="caller.py",
            defining_file="provider.py",
            detail="provider.py must define run_embedding",
            exact_key="exact",
            family_key="family",
        )
        plan = [
            SubTask(
                task_id="repair",
                description="repair API agreement",
                target_nodes=[NodeId("caller.py")],
                repair_obligations=(obligation,),
            )
        ]

        assert "must resolve=provider.py must define run_embedding" in render_plan(plan)


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


class TestProposedTasks:
    """Wave 7: MAK-proposed caller tasks are marked and can be dropped."""

    def _with_proposal(self) -> list[SubTask]:
        return [
            *_plan(),
            SubTask(
                task_id="mak.callers.1", description="update callers",
                target_nodes=[NodeId("c.py::function::c")], depends_on=["a"],
            ),
        ]

    def test_render_marks_proposed_tasks(self) -> None:
        proposed = frozenset({"mak.callers.1"})
        text = render_plan(self._with_proposal(), proposed=proposed)
        assert "[mak.callers.1] [proposed by MAK] update callers" in text
        assert "[a] do A" in text

    def test_drop_removes_them_and_asks_again(self) -> None:
        io = ScriptedIO(["d", "a"])
        result = display_plan_for_review(
            self._with_proposal(), prompt_fn=io.prompt, printer=io.printer,
            proposed=frozenset({"mak.callers.1"}),
        )
        assert [t.task_id for t in result] == ["a", "b"]
        assert "[d]rop MAK-proposed tasks" in io.output[1]
        assert any("Dropped 1 MAK-proposed task(s)" in line for line in io.output)
        # The second prompt no longer offers the choice.
        prompts = [line for line in io.output if line.startswith("Approve plan?")]
        assert "[d]rop" not in prompts[-1]

    def test_drop_is_not_offered_without_proposals(self) -> None:
        io = ScriptedIO(["d", "a"])
        display_plan_for_review(_plan(), prompt_fn=io.prompt, printer=io.printer)
        assert any("Unrecognized choice: 'd'" in line for line in io.output)

    def test_caller_findings_render_as_applied_or_advisory(self) -> None:
        findings = [
            PlanFinding("missing_caller", "a", "added: 'mak.callers.1' updates 'x'"),
            PlanFinding("missing_caller", "a", "'y' calls 'z'; no task updates it"),
        ]
        text = render_plan(_plan(), findings)
        assert "✎ [a] added: 'mak.callers.1'" in text
        assert "⚠ [a] 'y' calls" in text
