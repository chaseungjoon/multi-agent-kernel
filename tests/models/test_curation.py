from __future__ import annotations

import pytest

from mak.models.curation import (
    CURATED,
    DENY,
    Judgment,
    display_name_for,
    filter_ids,
    is_denied,
    judgment_for,
)


class TestJudgment:
    def test_defaults_are_neutral(self) -> None:
        """Neutral = usable, unstarred, unwarned. MAK holds no opinion."""
        j = Judgment()
        assert j.recommended is False
        assert j.planner_recommended is False
        assert j.planner_ok is True

    def test_curated_id_returns_its_flags(self) -> None:
        assert judgment_for("claude-opus-5").planner_recommended is True
        assert judgment_for("claude-sonnet-5").recommended is True
        assert judgment_for("claude-haiku-4-5").planner_ok is False

    @pytest.mark.parametrize(
        "model_id",
        ["claude-opus-9", "gpt-6", "gemini-4.0-pro", "claude-super-mega-5"],
    )
    def test_unknown_future_model_is_neutral(self, model_id: str) -> None:
        """A model that ships tomorrow is usable, but never auto-starred."""
        assert judgment_for(model_id) == Judgment()

    @pytest.mark.parametrize("model_id", ["", "   ", "!!!", "a" * 300])
    def test_garbage_ids_do_not_raise(self, model_id: str) -> None:
        assert judgment_for(model_id) == Judgment()

    def test_no_inference_from_id_shape(self) -> None:
        """Guard: nothing may infer judgment from a name that *looks* premium."""
        for model_id in ("claude-opus-99", "gpt-9-ultra", "gemini-9.9-pro"):
            j = judgment_for(model_id)
            assert j.recommended is False
            assert j.planner_recommended is False

    def test_only_curated_ids_carry_stars(self) -> None:
        """The regression test for 'MAK never judges a model'."""
        starred = {
            m
            for m, j in CURATED.items()
            if j.recommended or j.planner_recommended
        }
        for model_id in ("brand-new-model", "claude-opus-6", "gpt-7"):
            assert model_id not in starred
            j = judgment_for(model_id)
            assert not (j.recommended or j.planner_recommended)


class TestDeny:
    @pytest.mark.parametrize(
        "model_id",
        [
            "dall-e-3",
            "whisper-1",
            "tts-1-hd",
            "text-embedding-3-large",
            "omni-moderation-latest",
            "babbage-002",
            "davinci-002",
            "gpt-4o-audio-preview",
            "gpt-4o-realtime-preview",
            "gpt-4o-transcribe",
            "gpt-4o-search-preview",
            "gpt-3.5-turbo-instruct",
        ],
    )
    def test_openai_non_chat_endpoints_denied(self, model_id: str) -> None:
        assert is_denied("openai", model_id) is True

    @pytest.mark.parametrize(
        "model_id", ["gpt-5.6-sol", "gpt-5.5", "o3", "gpt-4o", "gpt-5.6-luna"]
    )
    def test_openai_chat_models_allowed(self, model_id: str) -> None:
        assert is_denied("openai", model_id) is False

    @pytest.mark.parametrize(
        "model_id",
        [
            "text-embedding-004",
            "aqa",
            "imagen-3.0-generate",
            "veo-2",
            "lyria-3-pro-preview",
            "gemini-2.5-flash-image",
            "gemini-3.1-flash-image-preview",
            "gemini-2.5-flash-preview-tts",
            "gemini-3.1-flash-tts-preview",
            "nano-banana-pro-preview",
        ],
    )
    def test_gemini_non_chat_denied(self, model_id: str) -> None:
        assert is_denied("gemini", model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            "gemini-3.5-flash",
            "gemini-3-pro-preview",
            "gemini-2.5-pro",
            "deep-research-preview-04-2026",
            "gemma-4-31b-it",
        ],
    )
    def test_gemini_chat_models_allowed(self, model_id: str) -> None:
        assert is_denied("gemini", model_id) is False

    @pytest.mark.parametrize(
        "model_id",
        [
            "gpt-4o-mini-tts-2025-03-20",
            "gpt-3.5-turbo-instruct-0914",
            "gpt-image-1",
        ],
    )
    def test_openai_dated_non_chat_variants_denied(self, model_id: str) -> None:
        """Live data showed dated suffixes slipping past anchored patterns."""
        assert is_denied("openai", model_id) is True

    def test_anthropic_denies_nothing(self) -> None:
        assert DENY["anthropic"] == ()
        assert is_denied("anthropic", "claude-opus-5") is False

    def test_unknown_provider_denies_nothing(self) -> None:
        assert is_denied("mystery", "anything") is False


class TestFilterIds:
    def test_drops_denied_and_keeps_order(self) -> None:
        out = filter_ids(
            "openai", ["gpt-5.6-sol", "dall-e-3", "gpt-5.5", "whisper-1"]
        )
        assert out == ["gpt-5.6-sol", "gpt-5.5"]

    def test_dated_snapshot_collapses_when_base_present(self) -> None:
        out = filter_ids(
            "anthropic", ["claude-haiku-4-5", "claude-haiku-4-5-20251001"]
        )
        assert out == ["claude-haiku-4-5"]

    def test_dated_snapshot_survives_without_base(self) -> None:
        """Dropping it would make the model unselectable — keep it."""
        out = filter_ids("anthropic", ["claude-sonnet-4-5-20250929"])
        assert out == ["claude-sonnet-4-5-20250929"]

    def test_known_alias_canonicalizes_a_dated_only_fetch(self) -> None:
        """Anthropic returns only the dated id; the alias is still valid."""
        out = filter_ids(
            "anthropic",
            ["claude-haiku-4-5-20251001"],
            known_aliases=["claude-haiku-4-5"],
        )
        assert out == ["claude-haiku-4-5"]

    def test_canonicalization_deduplicates(self) -> None:
        out = filter_ids(
            "anthropic",
            ["claude-haiku-4-5", "claude-haiku-4-5-20251001"],
            known_aliases=["claude-haiku-4-5"],
        )
        assert out == ["claude-haiku-4-5"]

    def test_unknown_alias_leaves_dated_id_alone(self) -> None:
        out = filter_ids(
            "anthropic",
            ["claude-opus-4-5-20251101"],
            known_aliases=["claude-haiku-4-5"],
        )
        assert out == ["claude-opus-4-5-20251101"]

    def test_hyphenated_date_form_collapses(self) -> None:
        out = filter_ids("openai", ["gpt-4o", "gpt-4o-2024-08-06"])
        assert out == ["gpt-4o"]

    def test_deduplicates(self) -> None:
        assert filter_ids("openai", ["gpt-5.5", "gpt-5.5"]) == ["gpt-5.5"]

    def test_drops_empty_ids(self) -> None:
        assert filter_ids("openai", ["", "gpt-5.5"]) == ["gpt-5.5"]

    def test_empty_input(self) -> None:
        assert filter_ids("openai", []) == []


class TestDisplayName:
    def test_prefers_provider_name(self) -> None:
        assert display_name_for("claude-opus-5", "Claude Opus 5") == "Claude Opus 5"

    def test_falls_back_to_id_verbatim(self) -> None:
        assert display_name_for("gpt-5.6-sol", "") == "gpt-5.6-sol"

    def test_blank_provider_name_falls_back(self) -> None:
        assert display_name_for("gpt-5.6-sol", "   ") == "gpt-5.6-sol"
