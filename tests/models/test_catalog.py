from __future__ import annotations

import pytest

from mak.bootstrap import _PROVIDER_TO_API
from mak.models.catalog import (
    PROVIDER_ADAPTER,
    PROVIDER_KEY_ENV,
    PROVIDER_ORDER,
    ModelEntry,
    load_seed,
    seed_path,
    with_judgment,
)
from mak.models.curation import Judgment


class TestModelEntry:
    def test_derived_provider_fields(self) -> None:
        entry = ModelEntry(
            provider="anthropic", model_id="x", display_name="X"
        )
        assert entry.api_key_env == "ANTHROPIC_API_KEY"
        assert entry.adapter_type == "anthropic_api"

    @pytest.mark.parametrize(
        ("provider", "env", "adapter"),
        [
            ("anthropic", "ANTHROPIC_API_KEY", "anthropic_api"),
            ("openai", "OPENAI_API_KEY", "openai_api"),
            ("gemini", "GEMINI_API_KEY", "gemini_api"),
        ],
    )
    def test_every_provider_derives(
        self, provider: str, env: str, adapter: str
    ) -> None:
        entry = ModelEntry(provider=provider, model_id="m", display_name="M")
        assert entry.api_key_env == env
        assert entry.adapter_type == adapter

    def test_unknown_provider_raises(self) -> None:
        entry = ModelEntry(provider="nope", model_id="m", display_name="M")
        with pytest.raises(ValueError, match="unknown provider"):
            _ = entry.api_key_env
        with pytest.raises(ValueError, match="unknown provider"):
            _ = entry.adapter_type

    def test_round_trip_preserves_facts(self) -> None:
        entry = ModelEntry(
            provider="gemini",
            model_id="gemini-x",
            display_name="Gemini X",
            context_window=1_000_000,
            max_output=64_000,
        )
        restored = ModelEntry.from_dict(entry.to_dict())
        assert restored.model_id == entry.model_id
        assert restored.display_name == entry.display_name
        assert restored.context_window == entry.context_window
        assert restored.max_output == entry.max_output

    def test_to_dict_omits_judgment(self) -> None:
        """Judgment is never persisted — it is re-joined from curation on load."""
        entry = ModelEntry(
            provider="anthropic",
            model_id="m",
            display_name="M",
            recommended=True,
            planner_recommended=True,
        )
        payload = entry.to_dict()
        assert "recommended" not in payload
        assert "planner_ok" not in payload
        assert "planner_recommended" not in payload

    def test_from_dict_defaults_display_name_to_id(self) -> None:
        entry = ModelEntry.from_dict({"provider": "openai", "model_id": "gpt-x"})
        assert entry.display_name == "gpt-x"

    def test_from_dict_tolerates_bad_limits(self) -> None:
        entry = ModelEntry.from_dict(
            {"provider": "openai", "model_id": "m", "context_window": "huge"}
        )
        assert entry.context_window is None


class TestSeed:
    def test_seed_file_exists(self) -> None:
        assert seed_path().is_file()

    def test_seed_covers_every_provider(self) -> None:
        seed = load_seed()
        assert seed
        for provider in PROVIDER_ORDER:
            assert any(e.provider == provider for e in seed), provider

    def test_seed_ids_are_unique(self) -> None:
        ids = [e.model_id for e in load_seed()]
        assert len(ids) == len(set(ids))


class TestProviderMapConsistency:
    def test_matches_bootstrap(self) -> None:
        """The duplicated provider maps must not drift from bootstrap's."""
        for provider, (adapter, key_env) in _PROVIDER_TO_API.items():
            if provider not in PROVIDER_ADAPTER:
                continue  # aliases such as "google" are bootstrap-only
            assert PROVIDER_ADAPTER[provider] == adapter
            assert PROVIDER_KEY_ENV[provider] == key_env

    def test_order_covers_all_providers(self) -> None:
        assert set(PROVIDER_ORDER) == set(PROVIDER_ADAPTER)


class TestWithJudgment:
    def test_applies_all_three_flags(self) -> None:
        entry = ModelEntry(provider="anthropic", model_id="m", display_name="M")
        out = with_judgment(
            entry,
            Judgment(recommended=True, planner_ok=False, planner_recommended=True),
        )
        assert (out.recommended, out.planner_ok, out.planner_recommended) == (
            True,
            False,
            True,
        )

    def test_leaves_facts_untouched(self) -> None:
        entry = ModelEntry(
            provider="anthropic",
            model_id="m",
            display_name="M",
            context_window=123,
        )
        out = with_judgment(entry, Judgment(recommended=True))
        assert out.context_window == 123
        assert out.display_name == "M"
