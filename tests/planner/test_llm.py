"""Tests for mak.planner.llm: concrete PlannerLLMs + the model-prefix dispatcher."""

from __future__ import annotations

from typing import Any

import pytest

import mak.planner.llm as llm_module
from mak.core.exceptions import PlannerFailedError
from mak.planner.llm import (
    AnthropicPlannerLLM,
    GeminiPlannerLLM,
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
        monkeypatch.setattr(llm_module, "_documented_output_limits", lambda: limits)

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
        llm_module._documented_output_limits.cache_clear()
        try:
            assert resolve_max_tokens("claude-opus-5") == 16384
        finally:
            llm_module._documented_output_limits.cache_clear()

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
