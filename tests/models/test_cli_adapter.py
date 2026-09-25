"""The CLI's view of the catalog: a thin adapter over an explicit registry."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cli.core.models import (
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    ModelInfo,
    all_models,
    default_planner_route,
    models_for_provider,
    providers_with_keys,
    recommended_planner_for_provider,
)
from cli.core.state import CliState

from mak.models import ModelEntry, ModelRegistry
from mak.models.manifest import Manifest, ProviderBlock, save_manifest

_THIRD_PARTY = ModelEntry(
    provider="openrouter",
    endpoint_id="openrouter",
    model_id="z-ai/glm-5.2",
    display_name="z-ai/glm-5.2",
    evaluated=False,
)


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    """Build a registry over the packaged seed plus one third-party block."""
    path = tmp_path / "models.json"
    save_manifest(
        Manifest(
            providers={
                "openrouter": ProviderBlock(
                    fetched_at=datetime(2026, 9, 1, tzinfo=UTC),
                    models=(_THIRD_PARTY,),
                )
            }
        ),
        path,
    )
    return ModelRegistry(manifest_path_=path, sources=())


class TestAdapterSurface:
    def test_model_info_is_the_kernel_entry(self) -> None:
        assert ModelInfo is ModelEntry

    def test_all_models_non_empty_without_manifest(self, tmp_path: Path) -> None:
        """The packaged seed is the offline floor — never an empty list."""
        empty = ModelRegistry(manifest_path_=tmp_path / "absent.json", sources=())
        assert all_models(empty)

    def test_the_fixture_holds_a_third_party_entry(
        self, registry: ModelRegistry
    ) -> None:
        assert any(e.provider == "openrouter" for e in all_models(registry))

    def test_no_entry_property_raises(self, registry: ModelRegistry) -> None:
        # Built-in entries answer; an endpoint's entry says "ask the endpoint".
        for entry in all_models(registry):
            if entry.provider in PROVIDER_ORDER:
                assert entry.api_key_env is not None
                assert entry.api_key_env.endswith("_API_KEY")
                assert entry.adapter_type is not None
                assert entry.adapter_type.endswith("_api")
            else:
                assert entry.api_key_env is None
                assert entry.adapter_type is None

    def test_a_built_in_entry_names_its_key_and_adapter(self) -> None:
        entry = ModelEntry(
            provider="anthropic", model_id="claude-opus-5", display_name="Opus 5"
        )
        assert entry.api_key_env == "ANTHROPIC_API_KEY"
        assert entry.adapter_type == "anthropic_api"

    def test_a_third_party_entry_answers_none(self) -> None:
        assert _THIRD_PARTY.api_key_env is None
        assert _THIRD_PARTY.adapter_type is None

    def test_models_for_provider_filters(self, registry: ModelRegistry) -> None:
        for provider in PROVIDER_ORDER:
            entries = models_for_provider(registry, provider)
            assert entries
            assert all(e.provider == provider for e in entries)

    def test_provider_display_covers_order(self) -> None:
        assert set(PROVIDER_DISPLAY) == set(PROVIDER_ORDER)

    def test_state_builds_its_registry_once_and_lazily(self) -> None:
        state = CliState()
        assert state.model_registry is None
        assert state.models() is state.models()

    def test_an_injected_registry_is_used(self, registry: ModelRegistry) -> None:
        assert CliState(model_registry=registry).models() is registry

    def test_no_module_level_registry(self) -> None:
        """Nothing captured at import time: no catalog list, no registry."""
        import cli.core.models as mod

        assert not hasattr(mod, "ALL_MODELS")
        assert not hasattr(mod, "_REGISTRY")


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
    def test_anthropic_is_the_curated_pick(self, registry: ModelRegistry) -> None:
        pick = recommended_planner_for_provider(registry, "anthropic")
        assert pick == "claude-opus-5"

    def test_each_provider_returns_one_of_its_models(
        self, registry: ModelRegistry
    ) -> None:
        for provider in PROVIDER_ORDER:
            pick = recommended_planner_for_provider(registry, provider)
            assert pick in {m.model_id for m in models_for_provider(registry, provider)}

    def test_unknown_provider_falls_back(self, registry: ModelRegistry) -> None:
        assert recommended_planner_for_provider(registry, "nope") == "claude-opus-5"

    def test_default_route_follows_the_first_key(
        self, registry: ModelRegistry
    ) -> None:
        route = default_planner_route(registry, {"OPENAI_API_KEY": "sk"})
        assert route.kind == "hosted"
        assert route.provider == "openai"
        assert default_planner_route(registry, {}).spec() == "anthropic:claude-opus-5"
