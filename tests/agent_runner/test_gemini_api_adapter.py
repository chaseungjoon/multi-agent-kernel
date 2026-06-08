"""Tests for mak.agent_runner.adapters.gemini_api_adapter with a mocked SDK."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter
from mak.core.exceptions import AgentError
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
    def __init__(self, parts: list[FakePart]) -> None:
        self.content = FakeContent(parts)


class FakeResponse:
    def __init__(self, candidates: list[FakeCandidate]) -> None:
        self.candidates = candidates


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
        assert call["model"] == "gemini-3-pro"
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

    def test_no_client_no_sdk_is_unhealthy(self) -> None:
        # google-genai is not installed in the test env, so lazy construction
        # fails and health_check reports False rather than raising.
        adapter = GeminiApiAdapter()
        assert adapter.health_check() is False

    def test_agent_type(self) -> None:
        adapter, _ = _adapter_with_result(task_id="t", success=True)
        assert adapter.agent_type == "gemini_api"
