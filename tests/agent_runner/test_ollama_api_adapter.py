"""Tests for the native Ollama adapter, driven by an injected fake client."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mak.agent_runner.adapters.ollama_api_adapter import (
    OllamaApiAdapter,
    estimate_tokens,
)
from mak.core.exceptions import (
    AgentContextExceededError,
    AgentProtocolError,
    AgentRefusedError,
    AgentTruncatedError,
)
from mak.local.ollama_client import (
    OllamaChatResponse,
    OllamaError,
    OllamaModel,
)

_GOOD = json.dumps(
    {
        "task_id": "t1",
        "success": True,
        "modified_fragments": [{"node_id": "m.py", "new_source": "x = 1\n"}],
    }
)


class FakeClient:
    """An OllamaClient stand-in: scripted chat replies, scriptable metadata."""

    def __init__(
        self,
        script: list[Any] | None = None,
        *,
        context_length: int | None = 32768,
        installed: tuple[str, ...] = ("qwen2.5-coder:14b",),
        version_error: str | None = None,
        show_error: str | None = None,
    ) -> None:
        self._script = list(script or [])
        self._context_length = context_length
        self._installed = installed
        self._version_error = version_error
        self._show_error = show_error
        self.calls: list[dict[str, Any]] = []
        self.shown: list[str] = []

    def version(self) -> str:
        if self._version_error:
            raise OllamaError(self._version_error)
        return "0.5.7"

    def list_models(self) -> list[OllamaModel]:
        return [OllamaModel(name=name) for name in self._installed]

    def show(self, model: str) -> OllamaModel:
        self.shown.append(model)
        if self._show_error:
            raise OllamaError(self._show_error)
        return OllamaModel(name=model, context_length=self._context_length)

    def chat(self, **kwargs: Any) -> OllamaChatResponse:
        self.calls.append(kwargs)
        item = self._script.pop(0) if self._script else _reply(_GOOD)
        if isinstance(item, Exception):
            raise item
        return item


def _reply(
    content: str,
    *,
    done_reason: str = "stop",
    prompt_tokens: int = 0,
    output_tokens: int = 0,
) -> OllamaChatResponse:
    return OllamaChatResponse(
        content=content,
        done_reason=done_reason,
        prompt_eval_count=prompt_tokens,
        eval_count=output_tokens,
    )


def _adapter(client: FakeClient, **kwargs: Any) -> OllamaApiAdapter:
    return OllamaApiAdapter(client=client, model="qwen2.5-coder:14b", **kwargs)


class TestRoundTrip:
    def test_a_well_formed_reply_becomes_a_task_result(self) -> None:
        adapter = _adapter(FakeClient())
        result = adapter.parse_result(adapter.send("{}"))
        assert result.task_id == "t1"
        assert result.success is True
        assert result.new_sources == {"m.py": "x = 1\n"}

    def test_format_task_serializes_the_bundle(self) -> None:
        from mak.core.types import TaskBundle

        adapter = _adapter(FakeClient())
        formatted = adapter.format_task(TaskBundle(task_id="t", description="d"))
        assert json.loads(formatted)["task_id"] == "t"

    def test_agent_type(self) -> None:
        assert _adapter(FakeClient()).agent_type == "ollama_api"


class TestStructuredOutput:
    def test_json_schema_is_the_default_and_sends_the_schema(self) -> None:
        client = FakeClient()
        _adapter(client).send("{}")
        fmt = client.calls[0]["format"]
        assert isinstance(fmt, dict)
        assert set(fmt["required"]) == {"task_id", "success"}

    def test_json_object_sends_the_literal_json(self) -> None:
        client = FakeClient()
        _adapter(client, structured_output="json_object").send("{}")
        assert client.calls[0]["format"] == "json"

    def test_none_sends_no_format(self) -> None:
        client = FakeClient()
        _adapter(client, structured_output="none").send("{}")
        assert client.calls[0]["format"] is None


class TestRequestOptions:
    def test_num_predict_is_always_sent(self) -> None:
        client = FakeClient()
        _adapter(client, max_tokens=4096).send("{}")
        assert client.calls[0]["options"]["num_predict"] == 4096

    def test_keep_alive_and_temperature_are_forwarded_only_when_set(self) -> None:
        bare = FakeClient()
        _adapter(bare).send("{}")
        assert bare.calls[0]["keep_alive"] is None
        assert "temperature" not in bare.calls[0]["options"]

        tuned = FakeClient()
        _adapter(tuned, keep_alive="30m", temperature=0.1).send("{}")
        assert tuned.calls[0]["keep_alive"] == "30m"
        assert tuned.calls[0]["options"]["temperature"] == 0.1


class TestContextSizing:
    """D11: the reason this adapter exists at all."""

    def test_auto_sized_num_ctx_grows_with_the_prompt(self) -> None:
        small = FakeClient()
        _adapter(small, max_tokens=1024).send("x" * 4_000)
        large = FakeClient()
        _adapter(large, max_tokens=1024).send("x" * 60_000)
        assert (
            large.calls[0]["options"]["num_ctx"]
            > small.calls[0]["options"]["num_ctx"]
        )

    def test_num_ctx_never_drops_below_the_floor(self) -> None:
        client = FakeClient()
        _adapter(client, max_tokens=256).send("{}")
        assert client.calls[0]["options"]["num_ctx"] == 4096

    def test_num_ctx_is_capped_at_the_models_real_limit(self) -> None:
        client = FakeClient(context_length=8192)
        _adapter(client, max_tokens=1024).send("x" * 20_000)
        assert client.calls[0]["options"]["num_ctx"] <= 8192

    def test_the_model_is_only_asked_once(self) -> None:
        client = FakeClient()
        adapter = _adapter(client)
        adapter.send("{}")
        adapter.send("{}")
        assert client.shown == ["qwen2.5-coder:14b"]

    def test_a_configured_num_ctx_is_used_verbatim(self) -> None:
        client = FakeClient()
        _adapter(client, num_ctx=16384, max_tokens=1024).send("{}")
        assert client.calls[0]["options"]["num_ctx"] == 16384

    def test_an_over_long_bundle_is_refused_not_truncated(self) -> None:
        client = FakeClient(context_length=8192)
        adapter = _adapter(client, max_tokens=4096)
        with pytest.raises(AgentContextExceededError) as excinfo:
            adapter.send("x" * 400_000)
        assert excinfo.value.retryable is False
        message = str(excinfo.value)
        assert "dependency_context_bytes" in message
        assert "cross_file_context_bytes" in message
        assert "8192" in message
        assert client.calls == []

    def test_exceeding_a_configured_num_ctx_is_a_hard_failure(self) -> None:
        client = FakeClient(context_length=131072)
        adapter = _adapter(client, num_ctx=4096, max_tokens=2048)
        with pytest.raises(AgentContextExceededError, match="num_ctx"):
            adapter.send("x" * 100_000)

    def test_a_server_that_cannot_report_a_window_still_runs(self) -> None:
        client = FakeClient(show_error="404")
        _adapter(client, max_tokens=1024).send("x" * 20_000)
        assert client.calls[0]["options"]["num_ctx"] >= 4096

    def test_the_estimate_is_the_documented_heuristic(self) -> None:
        assert estimate_tokens("x" * 400) == 100


class TestStopSignals:
    def test_length_is_a_truncation(self) -> None:
        client = FakeClient([_reply("{", done_reason="length")])
        with pytest.raises(AgentTruncatedError):
            _adapter(client).send("{}")
        assert len(client.calls) == 1

    def test_a_refusal_is_not_repaired(self) -> None:
        client = FakeClient([_reply("", done_reason="refusal")])
        with pytest.raises(AgentRefusedError):
            _adapter(client).send("{}")
        assert len(client.calls) == 1

    def test_usage_is_read_from_ollamas_field_names(self) -> None:
        client = FakeClient([_reply(_GOOD, prompt_tokens=1200, output_tokens=90)])
        adapter = _adapter(client)
        result = adapter.parse_result(adapter.send("{}"))
        assert result.usage == {"input_tokens": 1200, "output_tokens": 90}


class TestRepair:
    def test_a_malformed_reply_is_repaired_once(self) -> None:
        client = FakeClient(
            [
                _reply("Sure! Here you go.", prompt_tokens=800, output_tokens=8),
                _reply(_GOOD, prompt_tokens=60, output_tokens=40),
            ]
        )
        adapter = _adapter(client)
        result = adapter.parse_result(adapter.send("{}"))
        assert result.success is True
        assert result.repairs == 1
        assert result.usage == {"input_tokens": 860, "output_tokens": 48}
        assert len(client.calls) == 2
        follow_up = client.calls[1]["messages"]
        assert follow_up[-2]["role"] == "assistant"
        assert "modified_fragments" in follow_up[-1]["content"]

    def test_repair_attempts_zero_raises_after_one_call(self) -> None:
        client = FakeClient([_reply("prose")])
        with pytest.raises(AgentProtocolError):
            _adapter(client, repair_attempts=0).send("{}")
        assert len(client.calls) == 1

    def test_an_empty_reply_is_a_protocol_error(self) -> None:
        client = FakeClient([_reply("   ")])
        with pytest.raises(AgentProtocolError, match="no content"):
            _adapter(client, repair_attempts=0).send("{}")


class TestHealthCheck:
    def test_healthy_when_running_with_the_model_pulled(self) -> None:
        adapter = _adapter(FakeClient())
        assert adapter.health_check() is True
        assert adapter.health_detail() is None

    def test_server_down_says_so_and_names_the_endpoint(self) -> None:
        adapter = _adapter(
            FakeClient(version_error="cannot reach http://localhost:11434")
        )
        assert adapter.health_check() is False
        detail = adapter.health_detail()
        assert detail is not None
        assert "not running" in detail
        assert "http://localhost:11434" in detail

    def test_an_unpulled_model_gets_its_own_detail(self) -> None:
        adapter = _adapter(FakeClient(installed=("llama3.1:8b",)))
        assert adapter.health_check() is False
        detail = adapter.health_detail()
        assert detail is not None
        assert "not pulled" in detail
        assert "ollama pull qwen2.5-coder:14b" in detail
