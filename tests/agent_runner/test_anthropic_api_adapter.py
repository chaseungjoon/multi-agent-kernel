"""Tests for mak.agent_runner.adapters.anthropic_api_adapter with a mocked SDK."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
from mak.agent_runner.adapters.budget import resolve_agent_max_tokens
from mak.core.exceptions import (
    AgentError,
    AgentRefusedError,
    AgentResponseError,
    AgentTruncatedError,
)
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


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(
        self,
        content: list[FakeBlock],
        *,
        stop_reason: str | None = "tool_use",
        usage: FakeUsage | None = None,
    ) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


class FakeStream:
    """Stands in for the SDK's MessageStreamManager context manager."""

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.closed = False

    def __enter__(self) -> FakeStream:
        """Enter the stream context, as the SDK's manager does."""
        return self

    def __exit__(self, *_exc: Any) -> None:
        """Close the stream on exit."""
        self.closed = True

    def get_final_message(self) -> FakeResponse:
        return self._response


class FakeMessages:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        stream = FakeStream(self._response)
        self.streams.append(stream)
        return stream


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

    def test_retry_note_reaches_the_model(self) -> None:
        # 12.3a: the re-dispatch channel is only useful if it is actually sent.
        adapter, _ = _adapter_with_result(task_id="t1", success=True)
        bundle = TaskBundle(
            task_id="t1", description="do it", retry_note="return less output"
        )
        assert json.loads(adapter.format_task(bundle))["retry_note"] == (
            "return less output"
        )


class TestSend:
    def test_forces_result_tool(self) -> None:
        adapter, client = _adapter_with_result(task_id="t1", success=True)
        adapter.send("{}")
        (call,) = client.messages.calls
        assert call["tool_choice"] == {"type": "tool", "name": "submit_task_result"}
        assert call["model"] == "claude-sonnet-5"
        assert call["tools"][0]["name"] == "submit_task_result"

    def test_send_streams_and_closes_the_stream(self) -> None:
        # A real agent budget exceeds the SDK's non-streaming window, so the call
        # must go through messages.stream — the trap the planner hotfix hit.
        adapter, client = _adapter_with_result(task_id="t1", success=True)
        adapter.send("{}")
        assert client.messages.streams[0].closed is True

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


class TestOutputBudget:
    """12.1b / 12.5c — the budget must come from the catalog, not a constant."""

    def test_budget_is_catalog_resolved_not_8192(self) -> None:
        adapter, client = _adapter_with_result(task_id="t", success=True)
        adapter.send("{}")
        requested = client.messages.calls[0]["max_tokens"]
        assert requested == resolve_agent_max_tokens("claude-sonnet-5")
        # The regression this wave exists for: 8192 is 6% of the documented limit
        # and sits just under the size of a real whole-file rewrite.
        assert requested > 8192

    def test_explicit_budget_wins(self) -> None:
        client = FakeClient(FakeResponse([_result_block(task_id="t", success=True)]))
        adapter = AnthropicApiAdapter(client=client, max_tokens=4096)
        adapter.send("{}")
        assert client.messages.calls[0]["max_tokens"] == 4096


class TestDegradedResponses:
    """12.5a — every shape a real provider returns when generation goes wrong.

    The one thing none of them may do is produce a successful, empty result.
    """

    @staticmethod
    def _send(response: FakeResponse) -> None:
        AnthropicApiAdapter(client=FakeClient(response)).send("{}")

    def test_truncated_tool_use_is_a_typed_error(self) -> None:
        # Byte-for-byte the reproduction from the session log: the scalar fields
        # arrived, the modified_fragments array never did.
        response = FakeResponse(
            [_result_block(task_id="t1", success=True)],
            stop_reason="max_tokens",
            usage=FakeUsage(1000, 8192),
        )
        with pytest.raises(AgentTruncatedError) as excinfo:
            self._send(response)
        assert excinfo.value.stop_reason == "max_tokens"
        assert excinfo.value.usage == {"input_tokens": 1000, "output_tokens": 8192}
        assert excinfo.value.retryable is True

    def test_truncation_with_empty_input_is_a_typed_error(self) -> None:
        with pytest.raises(AgentTruncatedError):
            self._send(
                FakeResponse([_result_block()], stop_reason="max_tokens")
            )

    def test_truncation_beats_a_missing_tool_block(self) -> None:
        # A cut before the tool call even started must still read as a cut.
        with pytest.raises(AgentTruncatedError):
            self._send(FakeResponse([], stop_reason="max_tokens"))

    def test_refusal_is_not_retryable(self) -> None:
        with pytest.raises(AgentRefusedError) as excinfo:
            self._send(
                FakeResponse(
                    [_result_block(task_id="t1", success=True)],
                    stop_reason="refusal",
                )
            )
        assert excinfo.value.retryable is False

    def test_empty_content_raises(self) -> None:
        with pytest.raises(AgentError):
            self._send(FakeResponse([], stop_reason="end_turn"))

    def test_no_tool_use_block_raises(self) -> None:
        with pytest.raises(AgentError):
            self._send(FakeResponse([FakeBlock(type="text")], stop_reason="end_turn"))

    @pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
    def test_no_degraded_shape_yields_an_empty_success(self, stop_reason: str) -> None:
        adapter = AnthropicApiAdapter(
            client=FakeClient(
                FakeResponse(
                    [_result_block(task_id="t1", success=True)],
                    stop_reason=stop_reason,
                )
            )
        )
        with pytest.raises(AgentResponseError):
            adapter.parse_result(adapter.send("{}"))


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

    def test_stop_reason_and_usage_reach_the_task_result(self) -> None:
        # 12.1a: a *good* attempt carries the provider's signals too, so the log
        # can show what a reply cost and why it ended.
        client = FakeClient(
            FakeResponse(
                [_result_block(task_id="t1", success=True)],
                stop_reason="tool_use",
                usage=FakeUsage(1200, 350),
            )
        )
        adapter = AnthropicApiAdapter(client=client)
        result = adapter.parse_result(adapter.send("{}"))
        assert result.stop_reason == "tool_use"
        assert result.usage == {"input_tokens": 1200, "output_tokens": 350}

    def test_no_changes_required_survives_the_round_trip(self) -> None:
        adapter, _ = _adapter_with_result(
            task_id="t1", success=True, no_changes_required=True
        )
        assert adapter.parse_result(adapter.send("{}")).no_changes_required is True

    def test_result_tool_schema_requests_new_source(self) -> None:
        # The forced-output schema must actually ask the model for the rewritten
        # source, or a real agent's edit could never reach the store.
        adapter, client = _adapter_with_result(task_id="t", success=True)
        adapter.send("{}")
        schema = client.messages.calls[0]["tools"][0]["input_schema"]
        fragments = schema["properties"]["modified_fragments"]
        assert fragments["items"]["properties"]["new_source"]["type"] == "string"

    def test_result_tool_schema_offers_the_noop_assertion(self) -> None:
        # 12.2a: the model can only assert a no-op if it is asked for one.
        adapter, client = _adapter_with_result(task_id="t", success=True)
        adapter.send("{}")
        schema = client.messages.calls[0]["tools"][0]["input_schema"]
        assert schema["properties"]["no_changes_required"]["type"] == "boolean"


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
