"""Tests for mak.planner.llm: concrete PlannerLLMs + the model-prefix dispatcher."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

import mak.core.budget as budget_module
from mak.core.exceptions import PlannerFailedError
from mak.local.ollama_client import OllamaChatResponse, OllamaModel
from mak.planner.llm import (
    AnthropicPlannerLLM,
    GeminiPlannerLLM,
    OllamaPlannerLLM,
    OpenAiPlannerLLM,
    build_planner_llm,
    resolve_max_tokens,
)
from mak.planner.response import ResponseError, TruncatedResponseError


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _AnthropicResp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class _FakeStream:
    """Stands in for the SDK's MessageStreamManager context manager."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.closed = False

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.closed = True

    def get_final_message(self) -> Any:
        return self._response


class FakeAnthropicClient:
    def __init__(self, text: str) -> None:
        self.messages = self
        self._text = text
        self.calls: list[dict[str, Any]] = []
        self.streams: list[_FakeStream] = []

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        stream = _FakeStream(_AnthropicResp(self._text))
        self.streams.append(stream)
        return stream


class _Choice:
    def __init__(self, text: str) -> None:
        self.message = type("Msg", (), {"content": text})()


class _OpenAiResp:
    def __init__(self, text: str) -> None:
        self.choices = [_Choice(text)]


class FakeOpenAiClient:
    def __init__(self, text: str) -> None:
        self.chat = type("Chat", (), {"completions": self})()
        self._text = text

    def create(self, **kwargs: Any) -> _OpenAiResp:
        return _OpenAiResp(self._text)


class _GeminiResp:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeGeminiClient:
    def __init__(self, text: str) -> None:
        self.models = self
        self._text = text

    def generate_content(self, **kwargs: Any) -> _GeminiResp:
        return _GeminiResp(self._text)


class TestComplete:
    def test_anthropic_complete_returns_text(self) -> None:
        llm = AnthropicPlannerLLM(
            model="claude-sonnet-4-6", client=FakeAnthropicClient("PLAN-A")
        )
        assert llm.complete("hi") == "PLAN-A"

    def test_openai_complete_returns_text(self) -> None:
        llm = OpenAiPlannerLLM(model="gpt-4o", client=FakeOpenAiClient("PLAN-O"))
        assert llm.complete("hi") == "PLAN-O"

    def test_gemini_complete_returns_text(self) -> None:
        llm = GeminiPlannerLLM(
            model="gemini-3-pro", client=FakeGeminiClient("PLAN-G")
        )
        assert llm.complete("hi") == "PLAN-G"


class TestBuildPlannerLLM:
    def test_claude_prefix_builds_anthropic(self) -> None:
        assert isinstance(build_planner_llm("claude-sonnet-4-6"), AnthropicPlannerLLM)

    def test_gpt_prefix_builds_openai(self) -> None:
        assert isinstance(build_planner_llm("gpt-4o"), OpenAiPlannerLLM)

    def test_o_series_prefix_builds_openai(self) -> None:
        assert isinstance(build_planner_llm("o3-mini"), OpenAiPlannerLLM)

    def test_gemini_prefix_builds_gemini(self) -> None:
        assert isinstance(build_planner_llm("gemini-3-pro"), GeminiPlannerLLM)

    def test_unknown_model_raises(self) -> None:
        with pytest.raises(PlannerFailedError, match="cannot infer"):
            build_planner_llm("llama-3")


class _StopResp:
    def __init__(self, text: str, stop_reason: str) -> None:
        self.content = [_Block(text)]
        self.stop_reason = stop_reason


class StoppedAnthropicClient:
    def __init__(self, stop_reason: str) -> None:
        self.messages = self
        self._stop_reason = stop_reason

    def stream(self, **kwargs: Any) -> _FakeStream:
        return _FakeStream(_StopResp("[{partial", self._stop_reason))


class _FinishChoice:
    def __init__(self, text: str, finish_reason: str) -> None:
        self.message = type("Msg", (), {"content": text})()
        self.finish_reason = finish_reason


class FinishedOpenAiClient:
    def __init__(self, finish_reason: str) -> None:
        self.chat = type("Chat", (), {"completions": self})()
        self._finish_reason = finish_reason

    def create(self, **kwargs: Any) -> Any:
        return type(
            "Resp", (), {"choices": [_FinishChoice("[{partial", self._finish_reason)]}
        )()


class _Candidate:
    def __init__(self, finish_reason: object) -> None:
        self.finish_reason = finish_reason


class CandidateGeminiClient:
    def __init__(self, text: str, finish_reason: object) -> None:
        self.models = self
        self._text = text
        self._finish_reason = finish_reason

    def generate_content(self, **kwargs: Any) -> Any:
        return type(
            "Resp",
            (),
            {"text": self._text, "candidates": [_Candidate(self._finish_reason)]},
        )()


class _Enum:
    """Stands in for the SDK's FinishReason enum, whose str() is dotted."""

    def __str__(self) -> str:
        return "FinishReason.MAX_TOKENS"


class TestMaxTokens:
    @staticmethod
    def _limits(monkeypatch: pytest.MonkeyPatch, limits: dict[str, int]) -> None:
        monkeypatch.setattr(
            budget_module, "documented_output_limits", lambda: limits
        )

    def test_uses_the_models_documented_output_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._limits(monkeypatch, {"m": 20000})
        assert resolve_max_tokens("m") == 20000

    def test_a_large_limit_is_clamped_to_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._limits(monkeypatch, {"m": 128000})
        assert resolve_max_tokens("m") == 32000

    def test_a_small_limit_is_raised_to_the_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._limits(monkeypatch, {"m": 1024})
        assert resolve_max_tokens("m") == 4096

    def test_unknown_model_gets_the_default(self) -> None:
        assert resolve_max_tokens("some-model-shipped-tomorrow") == 16384

    def test_an_unreadable_catalog_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The catalog is an optimisation; losing it must not break planning."""
        import mak.models.registry as registry_module

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("no manifest")

        monkeypatch.setattr(registry_module, "ModelRegistry", boom)
        budget_module.documented_output_limits.cache_clear()
        try:
            assert resolve_max_tokens("claude-opus-5") == 16384
        finally:
            budget_module.documented_output_limits.cache_clear()

    def test_real_catalog_budget_beats_the_old_default(self) -> None:
        """The regression guard: 4096 tokens truncated real plans mid-string."""
        assert resolve_max_tokens("claude-opus-5") >= 16384

    def test_anthropic_sends_the_resolved_budget(self) -> None:
        client = FakeAnthropicClient("[]")
        llm = AnthropicPlannerLLM(model="claude-opus-5", client=client)
        llm.complete("hi")
        assert client.calls[0]["max_tokens"] == resolve_max_tokens("claude-opus-5")

    def test_explicit_budget_wins(self) -> None:
        client = FakeAnthropicClient("[]")
        AnthropicPlannerLLM(
            model="claude-opus-5", client=client, max_tokens=512
        ).complete("hi")
        assert client.calls[0]["max_tokens"] == 512


class TestTruncationDetection:
    def test_anthropic_max_tokens_stop_reason(self) -> None:
        llm = AnthropicPlannerLLM(
            model="claude-opus-5", client=StoppedAnthropicClient("max_tokens")
        )
        with pytest.raises(TruncatedResponseError, match="output limit"):
            llm.complete("hi")

    def test_anthropic_normal_stop_reason_returns_text(self) -> None:
        llm = AnthropicPlannerLLM(
            model="claude-opus-5", client=StoppedAnthropicClient("end_turn")
        )
        assert llm.complete("hi") == "[{partial"

    def test_anthropic_refusal_is_not_retryable(self) -> None:
        llm = AnthropicPlannerLLM(
            model="claude-fable-5", client=StoppedAnthropicClient("refusal")
        )
        with pytest.raises(PlannerFailedError, match="declined"):
            llm.complete("hi")

    def test_openai_length_finish_reason(self) -> None:
        llm = OpenAiPlannerLLM(
            model="gpt-5.6-sol", client=FinishedOpenAiClient("length")
        )
        with pytest.raises(TruncatedResponseError, match="output-token limit"):
            llm.complete("hi")

    def test_openai_stop_finish_reason_returns_text(self) -> None:
        llm = OpenAiPlannerLLM(model="gpt-5.6-sol", client=FinishedOpenAiClient("stop"))
        assert llm.complete("hi") == "[{partial"

    def test_gemini_max_tokens_enum(self) -> None:
        llm = GeminiPlannerLLM(
            model="gemini-3.5-flash", client=CandidateGeminiClient("[{p", _Enum())
        )
        with pytest.raises(TruncatedResponseError, match="output-token limit"):
            llm.complete("hi")

    def test_gemini_max_tokens_plain_string(self) -> None:
        llm = GeminiPlannerLLM(
            model="gemini-3.5-flash", client=CandidateGeminiClient("[{p", "MAX_TOKENS")
        )
        with pytest.raises(TruncatedResponseError):
            llm.complete("hi")

    def test_gemini_blocked_candidate_reports_its_reason(self) -> None:
        llm = GeminiPlannerLLM(
            model="gemini-3.5-flash", client=CandidateGeminiClient("", "SAFETY")
        )
        with pytest.raises(ResponseError, match="SAFETY"):
            llm.complete("hi")

    def test_gemini_normal_finish_returns_text(self) -> None:
        llm = GeminiPlannerLLM(
            model="gemini-3.5-flash", client=CandidateGeminiClient("PLAN", "STOP")
        )
        assert llm.complete("hi") == "PLAN"


class TestAnthropicStreams:
    """The output budget exceeds the SDK's non-streaming ceiling.

    A non-streaming request at a plan-sized ``max_tokens`` is rejected outright
    with "Streaming is required for operations that may take longer than 10
    minutes", so the planner backend must stream.
    """

    def test_uses_stream_not_create(self) -> None:
        client = FakeAnthropicClient("[]")
        assert not hasattr(client, "create")
        AnthropicPlannerLLM(model="claude-opus-5", client=client).complete("hi")
        assert len(client.calls) == 1

    def test_stream_context_is_closed(self) -> None:
        client = FakeAnthropicClient("[]")
        AnthropicPlannerLLM(model="claude-opus-5", client=client).complete("hi")
        assert client.streams[0].closed is True

    def test_resolved_budget_stays_streamable(self) -> None:
        """Any budget we resolve must be one the SDK will accept over a stream."""
        for model in ("claude-opus-5", "claude-haiku-4-5", "unknown-model"):
            assert resolve_max_tokens(model) <= 128000


class FakeOllamaClient:
    """An OllamaClient stand-in for the planner backend."""

    def __init__(
        self,
        content: str = '{"tasks": []}',
        *,
        done_reason: str = "stop",
        context_length: int | None = 32768,
        prompt_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self._content = content
        self._done_reason = done_reason
        self._context_length = context_length
        self._prompt_tokens = prompt_tokens
        self._output_tokens = output_tokens
        self.calls: list[dict[str, Any]] = []

    def show(self, model: str) -> OllamaModel:
        return OllamaModel(name=model, context_length=self._context_length)

    def chat(self, **kwargs: Any) -> OllamaChatResponse:
        self.calls.append(kwargs)
        return OllamaChatResponse(
            content=self._content,
            done_reason=self._done_reason,
            prompt_eval_count=self._prompt_tokens,
            eval_count=self._output_tokens,
        )


class TestBackendResolution:
    """Wave 15.11 (D3): explicit backend, then transport, then model name."""

    def test_backend_ollama_builds_the_native_planner(self) -> None:
        llm = build_planner_llm(
            "qwen2.5-coder:14b",
            backend="ollama",
            base_url="http://localhost:11434",
        )
        assert isinstance(llm, OllamaPlannerLLM)
        assert llm.base_url == "http://localhost:11434"

    def test_base_url_alone_routes_an_unknown_model_to_openai(self) -> None:
        # A local model id matches no prefix and would otherwise raise before a
        # single call; a base_url *is* a statement about the transport.
        llm = build_planner_llm(
            "Qwen/Qwen2.5-Coder-32B-Instruct", base_url="http://localhost:8000/v1"
        )
        assert isinstance(llm, OpenAiPlannerLLM)
        assert llm.base_url == "http://localhost:8000/v1"

    def test_an_explicit_backend_beats_the_model_prefix(self) -> None:
        llm = build_planner_llm("gpt-5.6-sol", backend="anthropic")
        assert isinstance(llm, AnthropicPlannerLLM)

    def test_an_explicit_backend_beats_the_base_url(self) -> None:
        llm = build_planner_llm(
            "qwen2.5-coder:14b", backend="ollama", base_url="http://h:11434"
        )
        assert isinstance(llm, OllamaPlannerLLM)

    def test_an_unknown_backend_is_rejected(self) -> None:
        with pytest.raises(PlannerFailedError, match="unknown planner backend"):
            build_planner_llm("m", backend="llamacpp")

    def test_the_unknown_model_message_names_the_two_settings(self) -> None:
        with pytest.raises(PlannerFailedError) as excinfo:
            build_planner_llm("qwen2.5-coder:14b")
        message = str(excinfo.value)
        assert "planner.backend" in message
        assert "planner.base_url" in message


class TestPlannerKeyIsNeverLeaked:
    def test_a_real_openai_key_is_never_forwarded_to_a_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D2 again, on the planner side. Same rule, same reason.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-secret")
        captured: dict[str, Any] = {}

        class FakeOpenAI:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        module = types.ModuleType("openai")
        module.OpenAI = FakeOpenAI  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "openai", module)

        llm = OpenAiPlannerLLM(model="m", base_url="http://evil.example/v1")
        llm._get_client()
        assert captured["api_key"] == "local"
        assert captured["base_url"] == "http://evil.example/v1"

    def test_a_configured_key_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        class FakeOpenAI:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        module = types.ModuleType("openai")
        module.OpenAI = FakeOpenAI  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "openai", module)

        OpenAiPlannerLLM(
            model="m", base_url="http://gw/v1", api_key="tok"
        )._get_client()
        assert captured["api_key"] == "tok"


class TestOllamaPlannerLLM:
    def test_complete_returns_the_reply_and_records_usage(self) -> None:
        client = FakeOllamaClient('{"tasks": []}', prompt_tokens=900, output_tokens=70)
        llm = OllamaPlannerLLM(model="qwen2.5-coder:14b", client=client)
        assert llm.complete("plan this") == '{"tasks": []}'
        assert llm.last_usage == {"input_tokens": 900, "output_tokens": 70}

    def test_no_format_is_requested(self) -> None:
        # The planner parses its own JSON through loads_json, which already
        # tolerates fences and prose.
        client = FakeOllamaClient()
        OllamaPlannerLLM(model="m", client=client).complete("plan")
        assert "format" not in client.calls[0]

    def test_num_ctx_is_sized_to_the_prompt(self) -> None:
        small = FakeOllamaClient()
        OllamaPlannerLLM(model="m", client=small).complete("plan")
        large = FakeOllamaClient()
        OllamaPlannerLLM(model="m", client=large).complete("x" * 40_000)
        assert (
            large.calls[0]["options"]["num_ctx"]
            > small.calls[0]["options"]["num_ctx"]
        )

    def test_a_length_finish_raises_truncated(self) -> None:
        client = FakeOllamaClient("{", done_reason="length")
        with pytest.raises(TruncatedResponseError):
            OllamaPlannerLLM(model="m", client=client).complete("plan")

    def test_an_oversized_inventory_is_refused_with_the_numbers(self) -> None:
        # Better a clear failure than a plan written for half a repository.
        client = FakeOllamaClient(context_length=8192)
        llm = OllamaPlannerLLM(model="m", client=client, max_tokens=4096)
        with pytest.raises(PlannerFailedError) as excinfo:
            llm.complete("x" * 400_000)
        message = str(excinfo.value)
        assert "8192" in message
        assert "context window" in message
        assert client.calls == []
