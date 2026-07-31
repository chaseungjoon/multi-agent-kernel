"""Tests for mak.agent_runner.adapters.openai_api_adapter with a mocked SDK."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.core.exceptions import (
    AgentError,
    AgentRefusedError,
    AgentTruncatedError,
)
from mak.core.types import NodeId, TaskBundle


class FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class FakeChoice:
    def __init__(self, content: str | None, finish_reason: str = "stop") -> None:
        self.message = FakeMessage(content)
        self.finish_reason = finish_reason


class FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeCompletion:
    def __init__(
        self, choices: list[FakeChoice], usage: FakeUsage | None = None
    ) -> None:
        self.choices = choices
        self.usage = usage


class FakeCompletions:
    def __init__(self, completion: FakeCompletion) -> None:
        self._completion = completion
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls.append(kwargs)
        return self._completion


class FakeChat:
    def __init__(self, completion: FakeCompletion) -> None:
        self.completions = FakeCompletions(completion)


class FakeClient:
    def __init__(self, completion: FakeCompletion) -> None:
        self.chat = FakeChat(completion)


def _client_returning(content: str | None) -> FakeClient:
    return FakeClient(FakeCompletion([FakeChoice(content)]))


def _adapter(content: str | None) -> tuple[OpenAiApiAdapter, FakeClient]:
    client = _client_returning(content)
    return OpenAiApiAdapter(client=client), client


class TestSend:
    def test_uses_json_mode(self) -> None:
        adapter, client = _adapter(json.dumps({"task_id": "t", "success": True}))
        adapter.send("{}")
        (call,) = client.chat.completions.calls
        assert call["response_format"] == {"type": "json_object"}
        assert call["model"] == "gpt-5.6-sol"

    def test_send_normalizes_payload(self) -> None:
        adapter, _ = _adapter(
            json.dumps(
                {
                    "task_id": "t1",
                    "success": True,
                    "modified_nodes": ["m.py::function::f"],
                }
            )
        )
        raw = adapter.send("{}")
        data = json.loads(raw)
        assert data["task_id"] == "t1"
        assert data["protocol_version"] == "1.0"

    def test_send_raises_on_no_content(self) -> None:
        adapter, _ = _adapter(None)
        with pytest.raises(AgentError, match="no content"):
            adapter.send("{}")

    def test_send_raises_on_invalid_json(self) -> None:
        adapter, _ = _adapter("not json")
        with pytest.raises(AgentError, match="not valid JSON"):
            adapter.send("{}")

    def test_send_raises_on_no_choices(self) -> None:
        adapter = OpenAiApiAdapter(client=FakeClient(FakeCompletion([])))
        with pytest.raises(AgentError, match="no choices"):
            adapter.send("{}")

    def test_custom_model(self) -> None:
        client = _client_returning(json.dumps({"task_id": "t", "success": True}))
        adapter = OpenAiApiAdapter(client=client, model="o3")
        adapter.send("{}")
        assert client.chat.completions.calls[0]["model"] == "o3"

    def test_no_cap_is_sent_by_default(self) -> None:
        # 12.1e: sending nothing inherits the model's own maximum, which is the
        # better default — the Anthropic adapter's fixed 8192 was the bug.
        adapter, client = _adapter(json.dumps({"task_id": "t", "success": True}))
        adapter.send("{}")
        assert "max_completion_tokens" not in client.chat.completions.calls[0]

    def test_configured_cap_is_forwarded(self) -> None:
        client = _client_returning(json.dumps({"task_id": "t", "success": True}))
        OpenAiApiAdapter(client=client, max_tokens=4096).send("{}")
        assert client.chat.completions.calls[0]["max_completion_tokens"] == 4096


class TestDegradedResponses:
    """12.5a — a cut or filtered OpenAI reply must never read as a result."""

    @staticmethod
    def _adapter_with(choice: FakeChoice, usage: FakeUsage | None = None) -> Any:
        return OpenAiApiAdapter(client=FakeClient(FakeCompletion([choice], usage)))

    def test_length_finish_reason_is_a_truncation(self) -> None:
        # The dangerous case is a cut that still happens to be valid JSON: the
        # reply decodes into a successful result carrying no work.
        choice = FakeChoice(
            json.dumps({"task_id": "t1", "success": True}), finish_reason="length"
        )
        with pytest.raises(AgentTruncatedError) as excinfo:
            self._adapter_with(choice, FakeUsage(900, 4096)).send("{}")
        assert excinfo.value.stop_reason == "length"
        assert excinfo.value.usage == {"input_tokens": 900, "output_tokens": 4096}

    def test_content_filter_is_not_retryable(self) -> None:
        choice = FakeChoice(None, finish_reason="content_filter")
        with pytest.raises(AgentRefusedError) as excinfo:
            self._adapter_with(choice).send("{}")
        assert excinfo.value.retryable is False

    def test_a_non_object_json_body_raises(self) -> None:
        adapter, _ = _adapter(json.dumps(["not", "an", "object"]))
        with pytest.raises(AgentError, match="not an object"):
            adapter.send("{}")

    def test_stop_reason_and_usage_reach_the_result(self) -> None:
        choice = FakeChoice(json.dumps({"task_id": "t1", "success": True}))
        adapter = self._adapter_with(choice, FakeUsage(120, 45))
        result = adapter.parse_result(adapter.send("{}"))
        assert result.stop_reason == "stop"
        assert result.usage == {"input_tokens": 120, "output_tokens": 45}


class TestParseResult:
    def test_round_trip(self) -> None:
        adapter, _ = _adapter(
            json.dumps(
                {
                    "task_id": "t1",
                    "success": True,
                    "modified_nodes": ["m.py::function::f"],
                    "error": None,
                }
            )
        )
        result = adapter.parse_result(adapter.send("{}"))
        assert result.task_id == "t1"
        assert result.success is True
        assert result.modified_nodes == [NodeId("m.py::function::f")]

    def test_failure_round_trip(self) -> None:
        adapter, _ = _adapter(
            json.dumps({"task_id": "t1", "success": False, "error": "nope"})
        )
        result = adapter.parse_result(adapter.send("{}"))
        assert result.success is False
        assert result.error == "nope"


class TestMisc:
    def test_format_task(self) -> None:
        adapter, _ = _adapter(json.dumps({"task_id": "t", "success": True}))
        formatted = adapter.format_task(TaskBundle(task_id="t", description="d"))
        assert json.loads(formatted)["task_id"] == "t"

    def test_health_check_with_client(self) -> None:
        adapter, _ = _adapter(json.dumps({"task_id": "t", "success": True}))
        assert adapter.health_check() is True

    def test_unhealthy_when_client_cannot_be_built(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = OpenAiApiAdapter()

        def boom() -> object:
            raise AgentError("no client")

        monkeypatch.setattr(adapter, "_get_client", boom)
        assert adapter.health_check() is False

    def test_agent_type(self) -> None:
        adapter, _ = _adapter(json.dumps({"task_id": "t", "success": True}))
        assert adapter.agent_type == "openai_api"
