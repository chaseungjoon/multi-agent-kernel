"""Tests for mak.agent_runner.adapters.openai_api_adapter with a mocked SDK."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.core.exceptions import (
    AgentError,
    AgentProtocolError,
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


class ScriptedCompletions:
    """A completions endpoint that returns (or raises) a scripted reply per call."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self._script.pop(0) if self._script else self._script
        if isinstance(item, Exception):
            raise item
        return item


class ScriptedChat:
    def __init__(self, script: list[Any]) -> None:
        self.completions = ScriptedCompletions(script)


class ScriptedClient:
    def __init__(self, script: list[Any]) -> None:
        self.chat = ScriptedChat(script)


def _built(adapter: OpenAiApiAdapter) -> dict[str, Any]:
    """Return the kwargs the adapter would hand ``openai.OpenAI(...)``.

    Stubs the SDK module rather than the adapter's ``_get_client``, because the
    key-forwarding rule under test lives inside ``_get_client`` itself.
    """
    captured: dict[str, Any] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    module = types.ModuleType("openai")
    module.OpenAI = FakeOpenAI  # type: ignore[attr-defined]
    saved = sys.modules.get("openai")
    sys.modules["openai"] = module
    try:
        adapter._client = None
        adapter._get_client()
    finally:
        if saved is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = saved
    return captured


def _reply(content: str | None, **usage: int) -> FakeCompletion:
    counts = FakeUsage(usage.get("prompt", 0), usage.get("completion", 0))
    return FakeCompletion([FakeChoice(content)], counts)


_GOOD = json.dumps({"task_id": "t", "success": True})


class TestLocalEndpoint:
    """Wave 15.3: the OpenAI adapter as a transport for a local server."""

    def test_base_url_reaches_the_sdk_constructor(self) -> None:
        adapter = OpenAiApiAdapter(
            base_url="http://localhost:8000/v1", agent_type="local_api"
        )
        assert _built(adapter)["base_url"] == "http://localhost:8000/v1"

    def test_placeholder_key_is_sent_when_none_is_configured(self) -> None:
        adapter = OpenAiApiAdapter(base_url="http://localhost:11434/v1")
        assert _built(adapter)["api_key"] == "local"

    def test_a_real_openai_key_in_the_environment_is_never_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE security property of the local transport (D2). If this test is
        # ever deleted, MAK can silently POST a user's real OpenAI key to
        # whatever host their config names. Do not remove it.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-secret")
        adapter = OpenAiApiAdapter(base_url="http://evil.example/v1")
        options = _built(adapter)
        assert options["api_key"] == "local"
        assert "sk-real-secret" not in json.dumps(options)

    def test_a_configured_key_wins_over_the_placeholder(self) -> None:
        adapter = OpenAiApiAdapter(base_url="http://gw.example/v1", api_key="tok")
        assert _built(adapter)["api_key"] == "tok"

    def test_cloud_adapter_omits_base_url_and_reads_no_env_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-secret")
        options = _built(OpenAiApiAdapter())
        assert "base_url" not in options
        # Unchanged cloud behaviour: with no key configured the SDK reads its own.
        assert "api_key" not in options

    def test_cap_field_name_switches_with_the_transport(self) -> None:
        cloud = OpenAiApiAdapter(client=_client_returning(_GOOD), max_tokens=99)
        cloud.send("{}")
        assert "max_completion_tokens" in cloud._client.chat.completions.calls[0]

        local = OpenAiApiAdapter(
            client=_client_returning(_GOOD),
            max_tokens=99,
            base_url="http://localhost:11434/v1",
        )
        local.send("{}")
        call = local._client.chat.completions.calls[0]
        assert call["max_tokens"] == 99
        assert "max_completion_tokens" not in call

    def test_local_api_instance_reports_its_own_agent_type(self) -> None:
        adapter = OpenAiApiAdapter(agent_type="local_api", base_url="http://h/v1")
        assert adapter.agent_type == "local_api"
        assert OpenAiApiAdapter().agent_type == "openai_api"


class _ListingModels:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.listed = 0

    def list(self) -> list[str]:
        self.listed += 1
        if self.fail:
            raise RuntimeError("connection refused")
        return []


class _ProbeClient:
    def __init__(self, fail: bool = False) -> None:
        self.models = _ListingModels(fail)

    def with_options(self, **_: Any) -> _ProbeClient:
        return self


class TestLocalHealthProbe:
    def test_probe_succeeds_against_a_listing_server(self) -> None:
        client = _ProbeClient()
        adapter = OpenAiApiAdapter(client=client, base_url="http://localhost:8000/v1")
        assert adapter.health_check() is True
        assert client.models.listed == 1
        assert adapter.health_detail() is None

    def test_probe_fails_and_names_the_endpoint(self) -> None:
        adapter = OpenAiApiAdapter(
            client=_ProbeClient(fail=True), base_url="http://localhost:8000/v1"
        )
        assert adapter.health_check() is False
        detail = adapter.health_detail()
        assert detail is not None and "http://localhost:8000/v1" in detail

    def test_cloud_health_check_makes_no_call(self) -> None:
        # "Building a registry performs no network call" must stay true for the
        # cloud path.
        client = _ProbeClient()
        assert OpenAiApiAdapter(client=client).health_check() is True
        assert client.models.listed == 0


class TestStructuredOutputModes:
    def test_json_object_is_the_default(self) -> None:
        adapter, client = _adapter(_GOOD)
        adapter.send("{}")
        assert client.chat.completions.calls[0]["response_format"] == {
            "type": "json_object"
        }

    def test_json_schema_sends_the_strict_schema(self) -> None:
        adapter = OpenAiApiAdapter(
            client=_client_returning(_GOOD), structured_output="json_schema"
        )
        adapter.send("{}")
        fmt = adapter._client.chat.completions.calls[0]["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        assert "task_id" in fmt["json_schema"]["schema"]["properties"]

    def test_none_sends_no_response_format(self) -> None:
        adapter = OpenAiApiAdapter(
            client=_client_returning(_GOOD), structured_output="none"
        )
        adapter.send("{}")
        assert "response_format" not in adapter._client.chat.completions.calls[0]

    def test_unsupported_format_downgrades_once(self) -> None:
        client = ScriptedClient(
            [RuntimeError("400: response_format json_schema is unsupported"),
             _reply(_GOOD)]
        )
        adapter = OpenAiApiAdapter(client=client, structured_output="json_schema")
        result = adapter.parse_result(adapter.send("{}"))
        assert result.success is True
        calls = client.chat.completions.calls
        assert len(calls) == 2
        assert calls[0]["response_format"]["type"] == "json_schema"
        assert calls[1]["response_format"] == {"type": "json_object"}

    def test_an_unrelated_error_propagates_after_exactly_one_call(self) -> None:
        client = ScriptedClient([RuntimeError("rate limited"), _reply(_GOOD)])
        adapter = OpenAiApiAdapter(client=client, structured_output="json_schema")
        with pytest.raises(RuntimeError, match="rate limited"):
            adapter.send("{}")
        assert len(client.chat.completions.calls) == 1


class TestProtocolClassification:
    """Wave 15.6 (D7): a malformed body is a protocol failure, not an api one."""

    @pytest.mark.parametrize(
        "content", [None, "not json at all", json.dumps([1, 2, 3])]
    )
    def test_malformed_body_raises_agent_protocol_error(
        self, content: str | None
    ) -> None:
        adapter = OpenAiApiAdapter(
            client=_client_returning(content), repair_attempts=0
        )
        with pytest.raises(AgentProtocolError):
            adapter.send("{}")

    def test_no_choices_raises_agent_protocol_error(self) -> None:
        client = FakeClient(FakeCompletion([]))
        with pytest.raises(AgentProtocolError, match="no choices"):
            OpenAiApiAdapter(client=client).send("{}")

    def test_rejected_body_keeps_its_usage(self) -> None:
        client = ScriptedClient([_reply("nonsense", prompt=7, completion=3)])
        adapter = OpenAiApiAdapter(client=client, repair_attempts=0)
        with pytest.raises(AgentProtocolError) as excinfo:
            adapter.send("{}")
        assert excinfo.value.usage == {"input_tokens": 7, "output_tokens": 3}


class TestRepairTurn:
    """Wave 15.7 (D6): one short follow-up instead of a whole-bundle re-dispatch."""

    def test_malformed_then_good_reports_one_repair(self) -> None:
        client = ScriptedClient(
            [_reply("here is your answer!", prompt=100, completion=10),
             _reply(_GOOD, prompt=20, completion=5)]
        )
        adapter = OpenAiApiAdapter(client=client)
        result = adapter.parse_result(adapter.send("{}"))
        assert result.success is True
        assert result.repairs == 1
        # Usage is summed across both turns: max_total_tokens is computed from
        # it, so a repair turn billing invisibly would put the ceiling out.
        assert result.usage == {"input_tokens": 120, "output_tokens": 15}
        assert len(client.chat.completions.calls) == 2

    def test_repair_message_carries_the_previous_reply_and_the_instruction(
        self,
    ) -> None:
        client = ScriptedClient([_reply("oops prose"), _reply(_GOOD)])
        OpenAiApiAdapter(client=client).send("{}")
        second = client.chat.completions.calls[1]["messages"]
        assert second[-2] == {"role": "assistant", "content": "oops prose"}
        assert "modified_fragments" in second[-1]["content"]
        assert second[-1]["role"] == "user"

    def test_repair_attempts_zero_raises_after_exactly_one_call(self) -> None:
        client = ScriptedClient([_reply("prose"), _reply(_GOOD)])
        adapter = OpenAiApiAdapter(client=client, repair_attempts=0)
        with pytest.raises(AgentProtocolError):
            adapter.send("{}")
        assert len(client.chat.completions.calls) == 1

    def test_two_malformed_replies_raise_with_both_turns_usage(self) -> None:
        client = ScriptedClient(
            [_reply("prose", prompt=10, completion=1),
             _reply("more prose", prompt=4, completion=2)]
        )
        adapter = OpenAiApiAdapter(client=client)
        with pytest.raises(AgentProtocolError) as excinfo:
            adapter.send("{}")
        assert excinfo.value.usage == {"input_tokens": 14, "output_tokens": 3}
        assert len(client.chat.completions.calls) == 2

    def test_a_truncated_reply_is_never_repaired(self) -> None:
        client = ScriptedClient(
            [FakeCompletion([FakeChoice("{\"task_id\"", "length")], FakeUsage(9, 9))]
        )
        adapter = OpenAiApiAdapter(client=client)
        with pytest.raises(AgentTruncatedError):
            adapter.send("{}")
        assert len(client.chat.completions.calls) == 1

    def test_a_refusal_is_never_repaired(self) -> None:
        client = ScriptedClient(
            [FakeCompletion([FakeChoice(None, "content_filter")], FakeUsage(1, 1))]
        )
        adapter = OpenAiApiAdapter(client=client)
        with pytest.raises(AgentRefusedError):
            adapter.send("{}")
        assert len(client.chat.completions.calls) == 1
