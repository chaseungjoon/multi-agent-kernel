"""Tests for mak.planner.planner."""

from __future__ import annotations

import json

import pytest

from mak.core.exceptions import PlannerFailedError
from mak.core.types import NodeId
from mak.planner.planner import Planner, parse_plan


class StubLLM:
    """An LLM stub that returns canned responses in sequence."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._responses:
            raise AssertionError("StubLLM ran out of responses")
        return self._responses.pop(0)


_VALID_PLAN = json.dumps(
    [
        {
            "task_id": "a",
            "description": "do A",
            "target_nodes": ["m.py::function::a"],
            "depends_on": [],
            "agent_type": "anthropic_api",
        },
        {
            "task_id": "b",
            "description": "do B",
            "target_nodes": ["m.py::function::b"],
            "depends_on": ["a"],
            "agent_type": "anthropic_api",
        },
    ]
)


class TestParsePlan:
    def test_valid_array(self) -> None:
        tasks = parse_plan(_VALID_PLAN)
        assert [t.task_id for t in tasks] == ["a", "b"]
        assert tasks[0].target_nodes == [NodeId("m.py::function::a")]
        assert tasks[1].depends_on == ["a"]

    def test_subtasks_wrapper_object(self) -> None:
        wrapped = json.dumps({"subtasks": json.loads(_VALID_PLAN)})
        assert len(parse_plan(wrapped)) == 2

    def test_code_fence_stripped(self) -> None:
        fenced = f"```json\n{_VALID_PLAN}\n```"
        assert len(parse_plan(fenced)) == 2

    def test_bare_fence_stripped(self) -> None:
        fenced = f"```\n{_VALID_PLAN}\n```"
        assert len(parse_plan(fenced)) == 2

    def test_malformed_json_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_plan("{not json")

    def test_non_array_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a JSON array"):
            parse_plan(json.dumps({"foo": "bar"}))

    def test_missing_task_id_raises(self) -> None:
        bad = json.dumps([{"description": "x"}])
        with pytest.raises(ValueError, match="task_id"):
            parse_plan(bad)

    def test_empty_description_raises(self) -> None:
        bad = json.dumps([{"task_id": "a", "description": ""}])
        with pytest.raises(ValueError, match="description"):
            parse_plan(bad)

    def test_target_nodes_must_be_strings(self) -> None:
        bad = json.dumps([{"task_id": "a", "description": "x", "target_nodes": [1]}])
        with pytest.raises(ValueError, match="target_nodes"):
            parse_plan(bad)

    def test_duplicate_task_id_raises(self) -> None:
        bad = json.dumps(
            [
                {"task_id": "a", "description": "x"},
                {"task_id": "a", "description": "y"},
            ]
        )
        with pytest.raises(ValueError, match="duplicate task_id"):
            parse_plan(bad)

    def test_unknown_dependency_raises(self) -> None:
        bad = json.dumps(
            [{"task_id": "a", "description": "x", "depends_on": ["ghost"]}]
        )
        with pytest.raises(ValueError, match="unknown task 'ghost'"):
            parse_plan(bad)

    def test_defaults_for_optional_fields(self) -> None:
        minimal = json.dumps([{"task_id": "a", "description": "x"}])
        (task,) = parse_plan(minimal)
        assert task.target_nodes == []
        assert task.context_nodes == []
        assert task.depends_on == []
        assert task.agent_type == ""

    def test_context_nodes_parsed(self) -> None:
        plan = json.dumps(
            [
                {
                    "task_id": "a",
                    "description": "x",
                    "target_nodes": ["m.py::method::C.m"],
                    "context_nodes": ["m.py::class::C"],
                }
            ]
        )
        (task,) = parse_plan(plan)
        assert task.context_nodes == [NodeId("m.py::class::C")]

    def test_context_nodes_must_be_strings(self) -> None:
        bad = json.dumps([{"task_id": "a", "description": "x", "context_nodes": [1]}])
        with pytest.raises(ValueError, match="context_nodes"):
            parse_plan(bad)


class TestDecompose:
    def test_valid_first_try(self) -> None:
        llm = StubLLM([_VALID_PLAN])
        tasks = Planner(llm).decompose("do stuff", [NodeId("m.py::function::a")])
        assert [t.task_id for t in tasks] == ["a", "b"]
        assert len(llm.prompts) == 1

    def test_prompt_includes_inventory(self) -> None:
        llm = StubLLM([_VALID_PLAN])
        Planner(llm).decompose("do stuff", [NodeId("m.py::class::Foo")])
        assert "m.py::class::Foo" in llm.prompts[0]
        assert "do stuff" in llm.prompts[0]

    def test_retries_after_malformed(self) -> None:
        llm = StubLLM(["not json", _VALID_PLAN])
        tasks = Planner(llm, max_retries=3).decompose("t", [])
        assert len(tasks) == 2
        assert len(llm.prompts) == 2
        # The retry prompt feeds back the rejection reason.
        assert "rejected" in llm.prompts[1]

    def test_exhausts_retries_raises(self) -> None:
        llm = StubLLM(["bad", "still bad", "nope"])
        with pytest.raises(PlannerFailedError, match="after 3 attempts"):
            Planner(llm, max_retries=3).decompose("t", [])

    def test_succeeds_on_last_attempt(self) -> None:
        llm = StubLLM(["bad", "bad", _VALID_PLAN])
        tasks = Planner(llm, max_retries=3).decompose("t", [])
        assert len(tasks) == 2

    def test_max_retries_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            Planner(StubLLM([]), max_retries=0)

    def test_empty_inventory_renders_placeholder(self) -> None:
        llm = StubLLM([_VALID_PLAN])
        Planner(llm).decompose("t", [])
        assert "(empty)" in llm.prompts[0]
