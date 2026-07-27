"""Tests for mak.planner.response: tolerant JSON extraction + truncation detection."""

from __future__ import annotations

import json

import pytest

from mak.planner.response import (
    EmptyResponseError,
    ResponseError,
    TruncatedResponseError,
    extract_json_payload,
    loads_json,
    repair_truncated,
)

_PLAN = [
    {
        "task_id": "t1",
        "description": "Add the authentication middleware",
        "target_nodes": ["app/auth.py::function::authenticate"],
        "context_nodes": [],
        "depends_on": [],
        "agent_type": "anthropic_api",
    },
    {
        "task_id": "t2",
        "description": "Wire the middleware into the router",
        "target_nodes": ["app/router.py::function::build"],
        "context_nodes": ["app/auth.py::function::authenticate"],
        "depends_on": ["t1"],
        "agent_type": "anthropic_api",
    },
]


def _truncate(value: object, *, at: int) -> str:
    """Serialize ``value`` and cut it off after ``at`` characters."""
    return json.dumps(value, indent=2)[:at]


class TestExtractJsonPayload:
    def test_bare_json_untouched(self) -> None:
        assert extract_json_payload('[{"a": 1}]') == '[{"a": 1}]'

    def test_leading_prose_dropped(self) -> None:
        raw = 'Here is the plan you asked for:\n[{"a": 1}]'
        assert extract_json_payload(raw) == '[{"a": 1}]'

    def test_fence_after_prose(self) -> None:
        raw = 'Sure!\n\n```json\n[{"a": 1}]\n```\n\nLet me know.'
        assert extract_json_payload(raw) == '[{"a": 1}]'

    def test_unclosed_fence_still_yields_body(self) -> None:
        """A reply cut off inside its fence must still reach the parser."""
        assert extract_json_payload('```json\n[{"a": 1') == '[{"a": 1'

    def test_object_root_found(self) -> None:
        assert extract_json_payload('note\n{"subtasks": []}') == '{"subtasks": []}'

    def test_empty_response_raises(self) -> None:
        with pytest.raises(EmptyResponseError):
            extract_json_payload("   \n ")

    def test_prose_only_raises(self) -> None:
        with pytest.raises(ResponseError, match="no JSON value"):
            extract_json_payload("I cannot help with that.")


class TestLoadsJson:
    def test_parses_plan(self) -> None:
        assert loads_json(json.dumps(_PLAN)) == _PLAN

    def test_trailing_prose_ignored(self) -> None:
        raw = json.dumps(_PLAN) + "\n\nThat covers both files."
        assert loads_json(raw) == _PLAN

    def test_malformed_is_not_reported_as_truncated(self) -> None:
        with pytest.raises(ResponseError) as excinfo:
            loads_json("{not json")
        assert not isinstance(excinfo.value, TruncatedResponseError)
        assert "not valid JSON" in str(excinfo.value)

    def test_truncated_plan_raises_truncated(self) -> None:
        with pytest.raises(TruncatedResponseError):
            loads_json(_truncate(_PLAN, at=400))

    def test_truncated_message_still_says_not_valid_json(self) -> None:
        """Callers (and the CLI) match on the existing wording."""
        with pytest.raises(TruncatedResponseError, match="not valid JSON"):
            loads_json(_truncate(_PLAN, at=400))

    def test_truncated_mid_string_is_detected(self) -> None:
        """The exact shape of the reported bug: a cut inside a description."""
        raw = '[{"task_id": "t1", "description": "Add the authentication mid'
        with pytest.raises(TruncatedResponseError):
            loads_json(raw)

    def test_truncation_counts_complete_elements(self) -> None:
        raw = json.dumps(_PLAN)[:-40]  # loses the tail of the second task
        with pytest.raises(TruncatedResponseError) as excinfo:
            loads_json(raw)
        assert excinfo.value.complete_elements == 1

    def test_partial_first_element_counts_zero(self) -> None:
        with pytest.raises(TruncatedResponseError) as excinfo:
            loads_json('[{"task_id": "t1", "descrip')
        assert excinfo.value.complete_elements == 0

    def test_truncated_is_a_value_error(self) -> None:
        """The planner's retry loop catches ValueError."""
        assert issubclass(TruncatedResponseError, ValueError)


class TestRepairTruncated:
    def test_complete_json_is_not_truncated(self) -> None:
        assert repair_truncated(json.dumps(_PLAN)) is None

    def test_malformed_but_balanced_is_not_truncated(self) -> None:
        assert repair_truncated('{"a" 1}') is None

    def test_repair_parses(self) -> None:
        repaired = repair_truncated(_truncate(_PLAN, at=400))
        assert repaired is not None
        json.loads(repaired[0])

    def test_elements_before_the_cut_survive_intact(self) -> None:
        """The repair recovers whole elements; the count reports how many."""
        repaired = repair_truncated(json.dumps(_PLAN)[:-40])
        assert repaired is not None
        recovered, complete = repaired
        assert json.loads(recovered)[0] == _PLAN[0]
        assert complete == 1

    def test_escaped_quote_does_not_confuse_the_scanner(self) -> None:
        raw = '[{"description": "he said \\"hi\\" and left", "task_id": "t'
        repaired = repair_truncated(raw)
        assert repaired is not None
        assert json.loads(repaired[0])[0]["description"] == 'he said "hi" and left'

    def test_dangling_backslash_is_dropped(self) -> None:
        repaired = repair_truncated('[{"description": "path C:\\\\')
        assert repaired is not None
        json.loads(repaired[0])

    def test_brace_inside_a_string_is_not_a_delimiter(self) -> None:
        raw = '[{"description": "use {curly} braces", "task_id": "t1"'
        repaired = repair_truncated(raw)
        assert repaired is not None
        assert json.loads(repaired[0])[0]["task_id"] == "t1"
