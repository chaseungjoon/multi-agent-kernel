"""The CLI's view of the catalog stays intact after the Wave 14 rewrite."""
from __future__ import annotations

from cli.core.models import (
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    ModelInfo,
    all_models,
    models_for_provider,
    providers_with_keys,
    recommended_planner_for_provider,
    registry,
)

from mak.models import ModelEntry


class TestAdapterSurface:
    def test_model_info_is_the_kernel_entry(self) -> None:
        assert ModelInfo is ModelEntry

    def test_all_models_non_empty_without_manifest(self) -> None:
        """The packaged seed is the offline floor — never an empty list."""
        assert all_models()

    def test_every_entry_resolves_its_key_env(self) -> None:
        # cli/commands.py reads model_info.api_key_env; it must never raise.
        for entry in all_models():
            assert entry.api_key_env.endswith("_API_KEY")
            assert entry.adapter_type.endswith("_api")

    def test_models_for_provider_filters(self) -> None:
        for provider in PROVIDER_ORDER:
            entries = models_for_provider(provider)
            assert entries
            assert all(e.provider == provider for e in entries)

    def test_provider_display_covers_order(self) -> None:
        assert set(PROVIDER_DISPLAY) == set(PROVIDER_ORDER)

    def test_registry_is_a_singleton(self) -> None:
        assert registry() is registry()

    def test_no_module_level_all_models_constant(self) -> None:
        """A list captured at import time could not reflect a refresh."""
        import cli.core.models as mod

        assert not hasattr(mod, "ALL_MODELS")


class TestProvidersWithKeys:
    def test_only_providers_with_non_empty_keys(self) -> None:
        keys = {
            "ANTHROPIC_API_KEY": "x",
            "OPENAI_API_KEY": "  ",
            "GEMINI_API_KEY": "",
        }
        assert providers_with_keys(keys) == ["anthropic"]

    def test_ignores_unknown_env_names(self) -> None:
        assert providers_with_keys({"WHATEVER_KEY": "x"}) == []


class TestRecommendedPlanner:
    def test_anthropic_is_the_curated_pick(self) -> None:
        assert recommended_planner_for_provider("anthropic") == "claude-opus-5"

    def test_each_provider_returns_one_of_its_models(self) -> None:
        for provider in PROVIDER_ORDER:
            pick = recommended_planner_for_provider(provider)
            assert pick in {m.model_id for m in models_for_provider(provider)}

    def test_unknown_provider_falls_back(self) -> None:
        assert recommended_planner_for_provider("nope") == "claude-opus-5"
