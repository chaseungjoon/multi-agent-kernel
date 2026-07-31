"""Tests for mak.agent_runner.adapters.gemini_api_adapter with a mocked SDK."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter
from mak.core.exceptions import (
    AgentError,
    AgentRefusedError,
    AgentTruncatedError,
)
from mak.core.types import NodeId, TaskBundle


class FakeFunctionCall:
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.name = name
        self.args = args


class FakePart:
    def __init__(self, function_call: FakeFunctionCall | None = None) -> None:
        self.function_call = function_call


class FakeContent:
    def __init__(self, parts: list[FakePart]) -> None:
        self.parts = parts


class FakeCandidate:
    def __init__(self, parts: list[FakePart], finish_reason: object = "STOP") -> None:
        self.content = FakeContent(parts)
        self.finish_reason = finish_reason


class FakeUsage:
    def __init__(self, prompt: int, candidates: int) -> None:
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates


class FakeFinishReasonEnum:
    """Stands in for the SDK enum whose str() is dotted."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __str__(self) -> str:
        """Render as the SDK's dotted enum text."""
        return f"FinishReason.{self._name}"


class FakeResponse:
    def __init__(
        self, candidates: list[FakeCandidate], usage: FakeUsage | None = None
    ) -> None:
        self.candidates = candidates
        self.usage_metadata = usage


class FakeModels:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return self._response


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.models = FakeModels(response)


def _result_part(**fields: Any) -> FakePart:
    return FakePart(FakeFunctionCall("submit_task_result", fields))


def _adapter_with_result(**fields: Any) -> tuple[GeminiApiAdapter, FakeClient]:
    client = FakeClient(FakeResponse([FakeCandidate([_result_part(**fields)])]))
    return GeminiApiAdapter(client=client), client


class TestFormatTask:
    def test_format_task_emits_bundle_json(self) -> None:
        adapter, _ = _adapter_with_result(task_id="t1", success=True)
        bundle = TaskBundle(task_id="t1", description="do it")
        data = json.loads(adapter.format_task(bundle))
        assert data["task_id"] == "t1"
        assert data["protocol_version"] == "1.0"


class TestSend:
    def test_forces_function_call(self) -> None:
        adapter, client = _adapter_with_result(task_id="t1", success=True)
        adapter.send("{}")
        (call,) = client.models.calls
        cfg = call["config"]
        assert call["model"] == "gemini-3.5-flash"
        fcc = cfg["tool_config"]["function_calling_config"]
        assert fcc["mode"] == "ANY"
        assert fcc["allowed_function_names"] == ["submit_task_result"]
        decl = cfg["tools"][0]["function_declarations"][0]
        assert decl["name"] == "submit_task_result"

    def test_send_extracts_function_payload(self) -> None:
        adapter, _ = _adapter_with_result(
            task_id="t1", success=True, modified_nodes=["m.py::function::f"]
        )
        data = json.loads(adapter.send("{}"))
        assert data["task_id"] == "t1"
        assert data["success"] is True
        assert data["protocol_version"] == "1.0"

    def test_send_raises_without_function_call(self) -> None:
        client = FakeClient(FakeResponse([FakeCandidate([FakePart(None)])]))
        adapter = GeminiApiAdapter(client=client)
        with pytest.raises(AgentError, match="no 'submit_task_result'"):
            adapter.send("{}")

    def test_send_raises_with_no_candidates(self) -> None:
        client = FakeClient(FakeResponse([]))
        adapter = GeminiApiAdapter(client=client)
        with pytest.raises(AgentError, match="no 'submit_task_result'"):
            adapter.send("{}")

    def test_custom_model_passed_through(self) -> None:
        client = FakeClient(
            FakeResponse([FakeCandidate([_result_part(task_id="t", success=True)])])
        )
        adapter = GeminiApiAdapter(client=client, model="gemini-3-flash")
        adapter.send("{}")
        assert client.models.calls[0]["model"] == "gemini-3-flash"

    def test_no_cap_is_sent_by_default(self) -> None:
        adapter, client = _adapter_with_result(task_id="t", success=True)
        adapter.send("{}")
        assert "max_output_tokens" not in client.models.calls[0]["config"]

    def test_configured_cap_is_forwarded(self) -> None:
        client = FakeClient(
            FakeResponse([FakeCandidate([_result_part(task_id="t", success=True)])])
        )
        GeminiApiAdapter(client=client, max_tokens=4096).send("{}")
        assert client.models.calls[0]["config"]["max_output_tokens"] == 4096

    def test_function_declaration_offers_the_noop_assertion(self) -> None:
        adapter, client = _adapter_with_result(task_id="t", success=True)
        adapter.send("{}")
        decl = client.models.calls[0]["config"]["tools"][0]["function_declarations"][0]
        props = decl["parameters"]["properties"]
        assert props["no_changes_required"]["type"] == "boolean"


class TestDegradedResponses:
    """12.5a — Gemini's function-call path truncates like Anthropic's."""

    @staticmethod
    def _adapter_with(
        candidate: FakeCandidate, usage: FakeUsage | None = None
    ) -> GeminiApiAdapter:
        return GeminiApiAdapter(client=FakeClient(FakeResponse([candidate], usage)))

    def test_max_tokens_enum_is_a_truncation(self) -> None:
        candidate = FakeCandidate(
            [_result_part(task_id="t1", success=True)],
            finish_reason=FakeFinishReasonEnum("MAX_TOKENS"),
        )
        with pytest.raises(AgentTruncatedError) as excinfo:
            self._adapter_with(candidate, FakeUsage(700, 8000)).send("{}")
        assert excinfo.value.usage == {"input_tokens": 700, "output_tokens": 8000}

    def test_max_tokens_plain_string_is_a_truncation(self) -> None:
        candidate = FakeCandidate(
            [_result_part(task_id="t1", success=True)], finish_reason="MAX_TOKENS"
        )
        with pytest.raises(AgentTruncatedError):
            self._adapter_with(candidate).send("{}")

    def test_safety_block_is_not_retryable(self) -> None:
        candidate = FakeCandidate([FakePart(None)], finish_reason="SAFETY")
        with pytest.raises(AgentRefusedError) as excinfo:
            self._adapter_with(candidate).send("{}")
        assert excinfo.value.retryable is False

    def test_recitation_block_is_not_retryable(self) -> None:
        candidate = FakeCandidate([FakePart(None)], finish_reason="RECITATION")
        with pytest.raises(AgentRefusedError):
            self._adapter_with(candidate).send("{}")

    def test_stop_reason_and_usage_reach_the_result(self) -> None:
        candidate = FakeCandidate([_result_part(task_id="t1", success=True)])
        adapter = self._adapter_with(candidate, FakeUsage(80, 20))
        result = adapter.parse_result(adapter.send("{}"))
        assert result.stop_reason == "STOP"
        assert result.usage == {"input_tokens": 80, "output_tokens": 20}


class TestParseResult:
    def test_round_trip_send_then_parse(self) -> None:
        adapter, _ = _adapter_with_result(
            task_id="t1", success=True, modified_nodes=["m.py::function::f"]
        )
        result = adapter.parse_result(adapter.send("{}"))
        assert result.task_id == "t1"
        assert result.success is True
        assert result.modified_nodes == [NodeId("m.py::function::f")]

    def test_failure_result_parsed(self) -> None:
        adapter, _ = _adapter_with_result(task_id="t1", success=False, error="boom")
        result = adapter.parse_result(adapter.send("{}"))
        assert result.success is False
        assert result.error == "boom"


class TestHealthCheck:
    def test_injected_client_is_healthy(self) -> None:
        adapter, _ = _adapter_with_result(task_id="t", success=True)
        assert adapter.health_check() is True

    def test_unhealthy_when_client_cannot_be_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Forced deterministically so the test does not depend on whether the SDK
        # (or an API key) happens to be present in the environment.
        adapter = GeminiApiAdapter()

        def boom() -> object:
            raise AgentError("no client")

        monkeypatch.setattr(adapter, "_get_client", boom)
        assert adapter.health_check() is False

    def test_agent_type(self) -> None:
        adapter, _ = _adapter_with_result(task_id="t", success=True)
        assert adapter.agent_type == "gemini_api"
