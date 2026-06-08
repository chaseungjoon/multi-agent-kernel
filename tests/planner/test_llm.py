"""Tests for mak.planner.llm: concrete PlannerLLMs + the model-prefix dispatcher."""

from __future__ import annotations

from typing import Any

import pytest

from mak.core.exceptions import PlannerFailedError
from mak.planner.llm import (
    AnthropicPlannerLLM,
    GeminiPlannerLLM,
    OpenAiPlannerLLM,
    build_planner_llm,
)


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _AnthropicResp:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text)]


class FakeAnthropicClient:
    def __init__(self, text: str) -> None:
        self.messages = self
        self._text = text
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _AnthropicResp:
        self.calls.append(kwargs)
        return _AnthropicResp(self._text)


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
