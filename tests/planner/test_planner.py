"""Tests for mak.planner.planner."""

from __future__ import annotations

import json

import pytest

from mak.core.exceptions import PlannerFailedError
from mak.core.types import NodeId
from mak.planner.planner import Planner, parse_plan
from mak.planner.response import TruncatedResponseError


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

    def test_non_python_target_rejected(self) -> None:
        bad = json.dumps(
            [{"task_id": "T01", "description": "x", "target_nodes": ["README.md"]}]
        )
        with pytest.raises(ValueError, match="only edits Python"):
            parse_plan(bad)

    def test_non_python_target_lists_offenders(self) -> None:
        bad = json.dumps(
            [
                {"task_id": "T01", "description": "x", "target_nodes": ["docs/a.md"]},
                {"task_id": "T02", "description": "y", "target_nodes": ["ok.py"]},
                {"task_id": "T03", "description": "z", "target_nodes": ["notes"]},
            ]
        )
        with pytest.raises(ValueError) as exc:
            parse_plan(bad)
        assert "T01 -> docs/a.md" in str(exc.value)
        assert "T03 -> notes" in str(exc.value)
        assert "ok.py" not in str(exc.value)  # the valid .py target is not flagged

    def test_two_tasks_writing_same_whole_file_rejected(self) -> None:
        bad = json.dumps([
            {"task_id": "a", "description": "x", "target_nodes": ["app.py"]},
            {"task_id": "b", "description": "y", "target_nodes": ["app.py"]},
        ])
        with pytest.raises(ValueError, match="both write the whole file"):
            parse_plan(bad)

    def test_whole_file_and_fragment_of_same_file_rejected(self) -> None:
        bad = json.dumps([
            {"task_id": "a", "description": "x", "target_nodes": ["m.py"]},
            {"task_id": "b", "description": "y",
             "target_nodes": ["m.py::function::f"]},
        ])
        with pytest.raises(ValueError, match="one granularity|both as a whole file"):
            parse_plan(bad)

    def test_same_file_different_symbols_pass(self) -> None:
        good = json.dumps([
            {"task_id": "a", "description": "x",
             "target_nodes": ["app.py::function::f"]},
            {"task_id": "b", "description": "y",
             "target_nodes": ["app.py::function::g"]},
        ])
        tasks = parse_plan(good)
        assert len(tasks) == 2

    def test_distinct_whole_files_pass(self) -> None:
        good = json.dumps([
            {"task_id": "a", "description": "x", "target_nodes": ["a.py"]},
            {"task_id": "b", "description": "y", "target_nodes": ["b.py"]},
        ])
        assert len(parse_plan(good)) == 2

    def test_python_targets_with_qualified_names_pass(self) -> None:
        good = json.dumps(
            [
                {"task_id": "a", "description": "x",
                 "target_nodes": ["pkg/mod.py::function::f", "main.py"]},
            ]
        )
        (task,) = parse_plan(good)
        assert task.target_nodes == [
            NodeId("pkg/mod.py::function::f"),
            NodeId("main.py"),
        ]

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

    def test_prompt_lists_configured_agent_types(self) -> None:
        llm = StubLLM([_VALID_PLAN])
        Planner(llm, agent_types=["openai_api", "gemini_api"]).decompose("t", [])
        assert "openai_api" in llm.prompts[0]
        assert "gemini_api" in llm.prompts[0]

    def test_prompt_omits_agent_section_when_unset(self) -> None:
        llm = StubLLM([_VALID_PLAN])
        Planner(llm).decompose("t", [])
        assert "CONFIGURED AGENT TYPES" not in llm.prompts[0]

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


_OUTLINE = json.dumps(
    [
        {"step_id": "core", "description": "build util", "files": ["util.py"],
         "depends_on": []},
        {"step_id": "app", "description": "build app", "files": ["app.py"],
         "depends_on": ["core"]},
    ]
)
_DETAIL_CORE = json.dumps(
    [
        {"task_id": "parse", "description": "write parse",
         "target_nodes": ["util.py::function::parse"], "depends_on": [],
         "agent_type": "anthropic_api"},
    ]
)
_DETAIL_APP = json.dumps(
    [
        {"task_id": "load", "description": "write load",
         "target_nodes": ["app.py::function::load"], "depends_on": [],
         "agent_type": "anthropic_api"},
        {"task_id": "run", "description": "write run",
         "target_nodes": ["app.py::function::run"], "depends_on": ["load"],
         "agent_type": "anthropic_api"},
    ]
)
_OUTLINE_INVENTORY = [
    NodeId("util.py::function::parse"),
    NodeId("app.py::function::load"),
    NodeId("app.py::function::run"),
]


class TestOutlineStrategy:
    def test_merges_namespaced_tasks_with_cross_and_intra_step_deps(self) -> None:
        llm = StubLLM([_OUTLINE, _DETAIL_CORE, _DETAIL_APP])
        planner = Planner(llm, strategy="outline")
        tasks = planner.decompose("build it", _OUTLINE_INVENTORY)

        by_id = {t.task_id: t for t in tasks}
        assert set(by_id) == {"s0.parse", "s1.load", "s1.run"}
        # Cross-step edge: every 'app' task depends on the upstream 'core' task.
        assert by_id["s1.load"].depends_on == ["s0.parse"]
        # Intra-step dep remapped to the namespaced id, plus the cross-step edge.
        assert by_id["s1.run"].depends_on == ["s1.load", "s0.parse"]
        assert by_id["s0.parse"].depends_on == []
        assert len(llm.prompts) == 3

    def test_detail_prompt_restricted_to_step_files(self) -> None:
        llm = StubLLM([_OUTLINE, _DETAIL_CORE, _DETAIL_APP])
        Planner(llm, strategy="outline").decompose("build it", _OUTLINE_INVENTORY)
        # The first detail prompt (for the 'core' step) sees only util.py symbols.
        core_prompt = llm.prompts[1]
        assert "util.py::function::parse" in core_prompt
        assert "app.py::function::load" not in core_prompt

    def test_malformed_outline_retries(self) -> None:
        llm = StubLLM(["not json", _OUTLINE, _DETAIL_CORE, _DETAIL_APP])
        tasks = Planner(llm, strategy="outline", max_retries=3).decompose(
            "build it", _OUTLINE_INVENTORY
        )
        assert set(t.task_id for t in tasks) == {"s0.parse", "s1.load", "s1.run"}
        assert "rejected" in llm.prompts[1]

    def test_outline_cycle_rejected(self) -> None:
        cyclic = json.dumps(
            [
                {"step_id": "a", "description": "x", "files": ["a.py"],
                 "depends_on": ["b"]},
                {"step_id": "b", "description": "y", "files": ["b.py"],
                 "depends_on": ["a"]},
            ]
        )
        llm = StubLLM([cyclic, cyclic, cyclic])
        with pytest.raises(PlannerFailedError, match="cycle"):
            Planner(llm, strategy="outline", max_retries=3).decompose("t", [])

    def test_oneshot_default_makes_one_call(self) -> None:
        llm = StubLLM([_VALID_PLAN])
        tasks = Planner(llm).decompose("t", [NodeId("m.py::function::a")])
        assert [t.task_id for t in tasks] == ["a", "b"]
        assert len(llm.prompts) == 1
        assert "OUTLINE mode" not in llm.prompts[0]


class TestSelfCritique:
    def test_verdict_ok_keeps_plan(self) -> None:
        llm = StubLLM([_VALID_PLAN, '{"verdict": "ok"}'])
        tasks = Planner(llm, self_critique=True).decompose("t", [])
        assert [t.task_id for t in tasks] == ["a", "b"]
        assert len(llm.prompts) == 2  # decompose + one critique call

    def test_corrected_array_adopted(self) -> None:
        corrected = json.dumps(
            [
                {"task_id": "a", "description": "do A",
                 "target_nodes": ["m.py::function::a"], "depends_on": [],
                 "agent_type": "anthropic_api"},
                {"task_id": "b", "description": "do B",
                 "target_nodes": ["m.py::function::b"], "depends_on": ["a"],
                 "agent_type": "anthropic_api"},
                {"task_id": "c", "description": "fix caller",
                 "target_nodes": ["m.py::function::c"], "depends_on": ["a"],
                 "agent_type": "anthropic_api"},
            ]
        )
        llm = StubLLM([_VALID_PLAN, corrected])
        tasks = Planner(llm, self_critique=True).decompose("t", [])
        assert [t.task_id for t in tasks] == ["a", "b", "c"]

    def test_garbage_critique_keeps_original(self) -> None:
        llm = StubLLM([_VALID_PLAN, "this is not json at all"])
        tasks = Planner(llm, self_critique=True).decompose("t", [])
        assert [t.task_id for t in tasks] == ["a", "b"]

    def test_invalid_corrected_plan_keeps_original(self) -> None:
        # A parseable JSON array that violates plan invariants is rejected -> keep.
        bad = json.dumps(
            [
                {"task_id": "a", "description": "x",
                 "target_nodes": ["m.py::function::a"], "depends_on": ["ghost"],
                 "agent_type": "anthropic_api"},
            ]
        )
        llm = StubLLM([_VALID_PLAN, bad])
        tasks = Planner(llm, self_critique=True).decompose("t", [])
        assert [t.task_id for t in tasks] == ["a", "b"]

    def test_no_critique_call_when_disabled(self) -> None:
        llm = StubLLM([_VALID_PLAN])
        Planner(llm).decompose("t", [])
        assert len(llm.prompts) == 1


class RaisingLLM:
    """An LLM stub that raises on the first ``failures`` calls, then answers."""

    def __init__(self, error: Exception, failures: int, then: str) -> None:
        self._error = error
        self._failures = failures
        self._then = then
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if len(self.prompts) <= self._failures:
            raise self._error
        return self._then


class TestTruncatedResponseRetry:
    """Truncation needs its own feedback.

    A cut-off response repeats verbatim on a naive retry, so the retry must ask
    for a *smaller* plan rather than a corrected one.
    """

    def test_truncated_plan_retries_with_a_compaction_note(self) -> None:
        truncated = _VALID_PLAN[:60]
        llm = StubLLM([truncated, _VALID_PLAN])
        tasks = Planner(llm, max_retries=3).decompose("t", [])
        assert [t.task_id for t in tasks] == ["a", "b"]
        assert "cut off by the output-token limit" in llm.prompts[1]
        assert "SMALLER plan" in llm.prompts[1]

    def test_malformed_still_gets_the_generic_note(self) -> None:
        llm = StubLLM(["{not json", _VALID_PLAN])
        Planner(llm, max_retries=3).decompose("t", [])
        assert "rejected" in llm.prompts[1]
        assert "SMALLER plan" not in llm.prompts[1]

    def test_exhausted_truncation_explains_the_budget(self) -> None:
        truncated = _VALID_PLAN[:60]
        llm = StubLLM([truncated, truncated, truncated])
        with pytest.raises(PlannerFailedError, match="output budget"):
            Planner(llm, max_retries=3).decompose("t", [])

    def test_provider_signalled_truncation_is_retried(self) -> None:
        """The Anthropic/OpenAI/Gemini backends raise before parsing happens."""
        llm = RaisingLLM(TruncatedResponseError("hit the cap"), 1, _VALID_PLAN)
        tasks = Planner(llm, max_retries=3).decompose("t", [])
        assert len(tasks) == 2
        assert "SMALLER plan" in llm.prompts[1]


class TestTransientCallFailures:
    def test_transient_error_is_retried(self) -> None:
        llm = RaisingLLM(RuntimeError("connection reset"), 2, _VALID_PLAN)
        planner = Planner(llm, max_retries=3)
        planner._sleep = lambda _seconds: None  # type: ignore[method-assign]
        assert len(planner.decompose("t", [])) == 2
        assert len(llm.prompts) == 3

    def test_exhausted_transient_errors_report_the_cause(self) -> None:
        llm = RaisingLLM(RuntimeError("rate limited"), 5, _VALID_PLAN)
        planner = Planner(llm, max_retries=3)
        planner._sleep = lambda _seconds: None  # type: ignore[method-assign]
        with pytest.raises(PlannerFailedError, match="rate limited"):
            planner.decompose("t", [])

    def test_setup_failure_is_not_retried(self) -> None:
        """A missing SDK will not fix itself; fail fast with its own message."""
        llm = RaisingLLM(PlannerFailedError("anthropic SDK not installed"), 5, "")
        with pytest.raises(PlannerFailedError, match="SDK not installed"):
            Planner(llm, max_retries=3).decompose("t", [])
        assert len(llm.prompts) == 1

    def test_transient_failures_back_off(self) -> None:
        llm = RaisingLLM(RuntimeError("boom"), 2, _VALID_PLAN)
        planner = Planner(llm, max_retries=3)
        slept: list[float] = []
        planner._sleep = slept.append  # type: ignore[method-assign]
        planner.decompose("t", [])
        assert slept == [1.0, 2.0]

    def test_rejected_plans_do_not_back_off(self) -> None:
        """Waiting cannot make a model answer better — only a call failure waits."""
        llm = StubLLM(["bad", "bad", _VALID_PLAN])
        planner = Planner(llm, max_retries=3)
        slept: list[float] = []
        planner._sleep = slept.append  # type: ignore[method-assign]
        planner.decompose("t", [])
        assert slept == []


class TestResponseFraming:
    def test_prose_around_the_plan_is_tolerated(self) -> None:
        llm = StubLLM([f"Here is the plan:\n{_VALID_PLAN}\nHope that helps!"])
        assert len(Planner(llm).decompose("t", [])) == 2

    def test_empty_response_is_retried_not_fatal(self) -> None:
        llm = StubLLM(["", _VALID_PLAN])
        assert len(Planner(llm, max_retries=3).decompose("t", [])) == 2


class TestCritiqueIsNeverFatal:
    def test_critique_call_failure_keeps_the_plan(self) -> None:
        llm = RaisingLLM(RuntimeError("api down"), 5, "")
        planner = Planner(StubLLM([_VALID_PLAN]), self_critique=True)
        tasks = planner.decompose("t", [])
        assert [t.task_id for t in tasks] == ["a", "b"]
        planner._llm = llm  # critique on an already-built plan
        assert [t.task_id for t in planner._critique_plan(tasks)] == ["a", "b"]

    def test_truncated_critique_keeps_the_plan(self) -> None:
        llm = StubLLM([_VALID_PLAN, _VALID_PLAN[:60]])
        tasks = Planner(llm, self_critique=True).decompose("t", [])
        assert [t.task_id for t in tasks] == ["a", "b"]
