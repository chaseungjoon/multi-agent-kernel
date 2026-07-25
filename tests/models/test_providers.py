"""Provider fetcher tests. No network: every SDK client is a local fake.

The fakes mirror the real SDK response shapes (verified against the installed
``anthropic``/``openai``/``google-genai`` packages), so a field rename upstream
shows up here rather than at runtime.
"""
from __future__ import annotations

from typing import Any

import pytest

from mak.models.providers import (
    AnthropicSource,
    GeminiSource,
    ModelFetchError,
    OpenAiSource,
    default_sources,
)


class _FakeModels:
    def __init__(self, items: list[Any], raises: Exception | None = None) -> None:
        self._items = items
        self._raises = raises

    def list(self) -> list[Any]:
        if self._raises is not None:
            raise self._raises
        return self._items


class _FakeClient:
    def __init__(self, items: list[Any], raises: Exception | None = None) -> None:
        self.models = _FakeModels(items, raises)


class _AnthropicModel:
    def __init__(
        self,
        model_id: str,
        display_name: str = "",
        max_input_tokens: int | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.id = model_id
        self.display_name = display_name
        self.max_input_tokens = max_input_tokens
        self.max_tokens = max_tokens


class _OpenAiModel:
    def __init__(self, model_id: str) -> None:
        self.id = model_id
        self.created = 0
        self.owned_by = "openai"


class _GeminiModel:
    def __init__(
        self,
        name: str,
        display_name: str = "",
        supported_actions: tuple[str, ...] = ("generateContent",),
        input_token_limit: int | None = None,
        output_token_limit: int | None = None,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self.supported_actions = supported_actions
        self.input_token_limit = input_token_limit
        self.output_token_limit = output_token_limit


def _patch_anthropic(
    monkeypatch: pytest.MonkeyPatch, items: list[Any], raises: Exception | None = None
) -> None:
    import anthropic

    monkeypatch.setattr(
        anthropic, "Anthropic", lambda **kw: _FakeClient(items, raises)
    )


def _patch_openai(
    monkeypatch: pytest.MonkeyPatch, items: list[Any], raises: Exception | None = None
) -> None:
    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **kw: _FakeClient(items, raises))


def _patch_gemini(
    monkeypatch: pytest.MonkeyPatch, items: list[Any], raises: Exception | None = None
) -> None:
    from google import genai

    monkeypatch.setattr(genai, "Client", lambda **kw: _FakeClient(items, raises))


class TestAnthropicSource:
    def test_parses_ids_names_and_limits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_anthropic(
            monkeypatch,
            [_AnthropicModel("claude-opus-5", "Claude Opus 5", 1_000_000, 128_000)],
        )
        models = AnthropicSource().fetch("key")
        assert len(models) == 1
        assert models[0].model_id == "claude-opus-5"
        assert models[0].display_name == "Claude Opus 5"
        assert models[0].context_window == 1_000_000
        assert models[0].max_output == 128_000

    def test_missing_limits_become_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_anthropic(monkeypatch, [_AnthropicModel("m")])
        assert AnthropicSource().fetch("key")[0].context_window is None

    def test_sdk_error_becomes_fetch_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_anthropic(monkeypatch, [], raises=RuntimeError("401 unauthorized"))
        with pytest.raises(ModelFetchError, match="anthropic"):
            AnthropicSource().fetch("bad-key")

    def test_provider_name(self) -> None:
        assert AnthropicSource().provider == "anthropic"


class TestOpenAiSource:
    def test_parses_bare_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_openai(
            monkeypatch, [_OpenAiModel("gpt-5.6-sol"), _OpenAiModel("dall-e-3")]
        )
        models = OpenAiSource().fetch("key")
        # No filtering happens here — curation.filter_ids owns that.
        assert [m.model_id for m in models] == ["gpt-5.6-sol", "dall-e-3"]

    def test_no_limits_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_openai(monkeypatch, [_OpenAiModel("gpt-5.5")])
        model = OpenAiSource().fetch("key")[0]
        assert model.context_window is None
        assert model.display_name == ""

    def test_sdk_error_becomes_fetch_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_openai(monkeypatch, [], raises=ConnectionError("offline"))
        with pytest.raises(ModelFetchError, match="openai"):
            OpenAiSource().fetch("key")


class TestGeminiSource:
    def test_strips_models_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_gemini(
            monkeypatch, [_GeminiModel("models/gemini-3.5-flash", "Gemini 3.5 Flash")]
        )
        models = GeminiSource().fetch("key")
        assert models[0].model_id == "gemini-3.5-flash"
        assert models[0].display_name == "Gemini 3.5 Flash"

    def test_drops_models_without_generate_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_gemini(
            monkeypatch,
            [
                _GeminiModel("models/gemini-3.5-flash"),
                _GeminiModel(
                    "models/text-embedding-004",
                    supported_actions=("embedContent",),
                ),
            ],
        )
        assert [m.model_id for m in GeminiSource().fetch("key")] == [
            "gemini-3.5-flash"
        ]

    def test_parses_token_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_gemini(
            monkeypatch,
            [
                _GeminiModel(
                    "models/gemini-x",
                    input_token_limit=1_000_000,
                    output_token_limit=64_000,
                )
            ],
        )
        model = GeminiSource().fetch("key")[0]
        assert model.context_window == 1_000_000
        assert model.max_output == 64_000

    def test_missing_supported_actions_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_gemini(
            monkeypatch, [_GeminiModel("models/weird", supported_actions=())]
        )
        assert GeminiSource().fetch("key") == []

    def test_sdk_error_becomes_fetch_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_gemini(monkeypatch, [], raises=ValueError("bad key"))
        with pytest.raises(ModelFetchError, match="gemini"):
            GeminiSource().fetch("key")


class TestDefaultSources:
    def test_one_per_provider_in_order(self) -> None:
        assert [s.provider for s in default_sources()] == [
            "anthropic",
            "openai",
            "gemini",
        ]
