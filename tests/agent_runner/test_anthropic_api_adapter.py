"""Tests for mak.agent_runner.adapters.anthropic_api_adapter with a mocked SDK."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
from mak.core.exceptions import AgentError
from mak.core.types import NodeId, TaskBundle


class FakeBlock:
    """Stand-in for an Anthropic content block."""

    def __init__(
        self,
        *,
        type: str,
        name: str | None = None,
        input: dict[str, Any] | None = None,
    ) -> None:
        self.type = type
        self.name = name
        self.input = input or {}


class FakeResponse:
    def __init__(self, content: list[FakeBlock]) -> None:
        self.content = content


class FakeMessages:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return self._response


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.messages = FakeMessages(response)


def _result_block(**fields: Any) -> FakeBlock:
    return FakeBlock(type="tool_use", name="submit_task_result", input=fields)


def _adapter_with_result(**fields: Any) -> tuple[AnthropicApiAdapter, FakeClient]:
    client = FakeClient(FakeResponse([_result_block(**fields)]))
    return AnthropicApiAdapter(client=client), client


class TestFormatTask:
    def test_format_task_emits_bundle_json(self) -> None:
        adapter, _ = _adapter_with_result(task_id="t1", success=True)
        bundle = TaskBundle(task_id="t1", description="do it")
        formatted = adapter.format_task(bundle)
        data = json.loads(formatted)
        assert data["task_id"] == "t1"
        assert data["protocol_version"] == "1.0"


class TestSend:
    def test_forces_result_tool(self) -> None:
        adapter, client = _adapter_with_result(task_id="t1", success=True)
        adapter.send("{}")
        (call,) = client.messages.calls
        assert call["tool_choice"] == {"type": "tool", "name": "submit_task_result"}
        assert call["model"] == "claude-sonnet-4-6"
        assert call["tools"][0]["name"] == "submit_task_result"

    def test_send_extracts_tool_payload(self) -> None:
        adapter, _ = _adapter_with_result(
            task_id="t1", success=True, modified_nodes=["m.py::function::f"]
        )
        raw = adapter.send("{}")
        data = json.loads(raw)
        assert data["task_id"] == "t1"
        assert data["success"] is True
        assert data["protocol_version"] == "1.0"

    def test_send_raises_without_tool_block(self) -> None:
        client = FakeClient(FakeResponse([FakeBlock(type="text")]))
        adapter = AnthropicApiAdapter(client=client)
        with pytest.raises(AgentError, match="no 'submit_task_result'"):
            adapter.send("{}")

    def test_custom_model_passed_through(self) -> None:
        client = FakeClient(FakeResponse([_result_block(task_id="t", success=True)]))
        adapter = AnthropicApiAdapter(client=client, model="claude-opus-4")
        adapter.send("{}")
        assert client.messages.calls[0]["model"] == "claude-opus-4"


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
        adapter, _ = _adapter_with_result(
            task_id="t1", success=False, error="boom"
        )
        result = adapter.parse_result(adapter.send("{}"))
        assert result.success is False
        assert result.error == "boom"

    def test_modified_fragments_become_new_sources(self) -> None:
        # The real transport: the model returns full rewritten source per node,
        # which must surface as TaskResult.new_sources for the session to stage.
        adapter, _ = _adapter_with_result(
            task_id="t1",
            success=True,
            modified_fragments=[
                {
                    "node_id": "m.py::function::f",
                    "new_source": "def f():\n    return 42\n",
                }
            ],
        )
        result = adapter.parse_result(adapter.send("{}"))
        assert result.modified_nodes == [NodeId("m.py::function::f")]
        assert result.new_sources == {
            NodeId("m.py::function::f"): "def f():\n    return 42\n"
        }

    def test_result_tool_schema_requests_new_source(self) -> None:
        # The forced-output schema must actually ask the model for the rewritten
        # source, or a real agent's edit could never reach the store.
        adapter, client = _adapter_with_result(task_id="t", success=True)
        adapter.send("{}")
        schema = client.messages.calls[0]["tools"][0]["input_schema"]
        fragments = schema["properties"]["modified_fragments"]
        assert fragments["items"]["properties"]["new_source"]["type"] == "string"


class TestHealthCheck:
    def test_injected_client_is_healthy(self) -> None:
        adapter, _ = _adapter_with_result(task_id="t", success=True)
        assert adapter.health_check() is True

    def test_unhealthy_when_client_cannot_be_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If constructing the SDK client fails (missing SDK or key), health_check
        # reports False rather than raising — forced deterministically here so the
        # test does not depend on whether the SDK happens to be installed.
        adapter = AnthropicApiAdapter()

        def boom() -> object:
            raise AgentError("no client")

        monkeypatch.setattr(adapter, "_get_client", boom)
        assert adapter.health_check() is False

    def test_agent_type(self) -> None:
        adapter, _ = _adapter_with_result(task_id="t", success=True)
        assert adapter.agent_type == "anthropic_api"
