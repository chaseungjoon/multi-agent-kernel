from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mak.models.manifest import Manifest, save_manifest
from mak.models.providers import ModelFetchError
from mak.models.registry import NO_REFRESH_ENV, ModelRegistry, refresh_disabled_by_env
from tests.models.test_refresh import FakeSource

KEYS = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "GEMINI_API_KEY": "g"}
NO_KEYS = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "GEMINI_API_KEY": ""}


def _registry(tmp_path: Path, sources: list[FakeSource] | None = None) -> ModelRegistry:
    return ModelRegistry(
        manifest_path_=tmp_path / "models.json", sources=sources or []
    )


class TestLoad:
    def test_seed_only_when_no_manifest(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        models = reg.all_models()
        assert models
        assert any(m.model_id == "claude-opus-5" for m in models)
        assert all(m.source == "seed" for m in models)

    def test_judgment_is_joined_from_curation(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path)
        opus5 = reg.find("claude-opus-5")
        sonnet5 = reg.find("claude-sonnet-5")
        haiku = reg.find("claude-haiku-4-5")
        assert opus5 is not None and opus5.planner_recommended is True
        assert sonnet5 is not None and sonnet5.recommended is True
        assert haiku is not None and haiku.planner_ok is False

    def test_grouped_by_provider_in_order(self, tmp_path: Path) -> None:
        providers = [m.provider for m in _registry(tmp_path).all_models()]
        assert providers == sorted(
            providers, key=lambda p: ["anthropic", "openai", "gemini"].index(p)
        )

    def test_manifest_extends_seed(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, [FakeSource("anthropic", ["claude-opus-77"])])
        reg.refresh_now(KEYS)
        assert reg.find("claude-opus-77") is not None

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        first = _registry(tmp_path, [FakeSource("anthropic", ["claude-opus-77"])])
        first.refresh_now(KEYS)
        second = _registry(tmp_path)
        assert second.find("claude-opus-77") is not None

    def test_corrupt_manifest_falls_back_to_seed(self, tmp_path: Path) -> None:
        (tmp_path / "models.json").write_text("{broken", encoding="utf-8")
        reg = _registry(tmp_path)
        assert reg.find("claude-opus-5") is not None


class TestRetired:
    def test_seed_model_absent_from_fetch_is_marked_retired(
        self, tmp_path: Path
    ) -> None:
        reg = _registry(tmp_path, [FakeSource("anthropic", ["claude-opus-5"])])
        reg.refresh_now(KEYS)
        haiku = reg.find("claude-haiku-4-5")
        assert haiku is not None
        assert haiku.retired is True

    def test_retired_models_sort_last(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, [FakeSource("anthropic", ["claude-opus-5"])])
        reg.refresh_now(KEYS)
        anthropic = reg.for_provider("anthropic")
        retired_flags = [m.retired for m in anthropic]
        assert retired_flags == sorted(retired_flags)

    def test_fetched_models_are_not_retired(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, [FakeSource("anthropic", ["claude-opus-5"])])
        reg.refresh_now(KEYS)
        opus = reg.find("claude-opus-5")
        assert opus is not None and opus.retired is False


class TestRecommendedPlanner:
    def test_prefers_planner_recommended(self, tmp_path: Path) -> None:
        assert _registry(tmp_path).recommended_planner("anthropic") == "claude-opus-5"

    def test_falls_back_to_recommended(self, tmp_path: Path) -> None:
        assert _registry(tmp_path).recommended_planner("openai") == "gpt-5.6-sol"

    def test_falls_back_when_curated_pick_removed(self, tmp_path: Path) -> None:
        """Removing the curated favourite degrades gracefully, never raises."""
        reg = _registry(
            tmp_path, [FakeSource("anthropic", ["claude-something-new"])]
        )
        reg.refresh_now(KEYS)
        assert reg.recommended_planner("anthropic") == "claude-something-new"

    def test_unknown_provider_returns_empty(self, tmp_path: Path) -> None:
        assert _registry(tmp_path).recommended_planner("nope") == ""


class TestRefreshNow:
    def test_swaps_snapshot_in_place(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, [FakeSource("openai", ["gpt-9"])])
        assert reg.find("gpt-9") is None
        reg.refresh_now(KEYS)
        assert reg.find("gpt-9") is not None

    def test_returns_report(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, [FakeSource("openai", ["gpt-9"])])
        report = reg.refresh_now(KEYS)
        assert report.ok is True
        assert "gpt-9" in report.results[0].added

    def test_failure_leaves_catalog_intact(self, tmp_path: Path) -> None:
        reg = _registry(
            tmp_path, [FakeSource("anthropic", raises=ModelFetchError("offline"))]
        )
        before = reg.all_models()
        reg.refresh_now(KEYS)
        assert reg.all_models() == before

    def test_writes_manifest_file(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, [FakeSource("openai", ["gpt-9"])])
        reg.refresh_now(KEYS)
        assert (tmp_path / "models.json").is_file()


class TestAutoRefresh:
    def test_starts_when_due(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, [FakeSource("openai", ["gpt-9"])])
        assert reg.maybe_auto_refresh(KEYS) is True
        for _ in range(200):  # let the daemon thread land
            if reg.find("gpt-9") is not None:
                break
            time.sleep(0.01)
        assert reg.find("gpt-9") is not None

    def test_noop_when_disabled(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, [FakeSource("openai", ["gpt-9"])])
        assert reg.maybe_auto_refresh(KEYS, enabled=False) is False

    def test_noop_without_keys(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path, [FakeSource("openai", ["gpt-9"])])
        assert reg.maybe_auto_refresh(NO_KEYS) is False

    def test_noop_when_env_opt_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(NO_REFRESH_ENV, "1")
        reg = _registry(tmp_path, [FakeSource("openai", ["gpt-9"])])
        assert reg.maybe_auto_refresh(KEYS) is False

    def test_noop_when_not_due(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        save_manifest(
            Manifest(last_refresh=now, last_attempt=now),
            tmp_path / "models.json",
        )
        reg = _registry(tmp_path, [FakeSource("openai", ["gpt-9"])])
        assert reg.maybe_auto_refresh(KEYS) is False

    def test_noop_when_in_cooldown(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        save_manifest(
            # Due (never refreshed) but attempted minutes ago -> cooldown blocks.
            Manifest(last_refresh=None, last_attempt=now - timedelta(minutes=5)),
            tmp_path / "models.json",
        )
        reg = _registry(tmp_path, [FakeSource("openai", ["gpt-9"])])
        assert reg.maybe_auto_refresh(KEYS) is False

    def test_background_failure_is_contained(self, tmp_path: Path) -> None:
        reg = _registry(
            tmp_path, [FakeSource("openai", raises=RuntimeError("boom"))]
        )
        before = reg.all_models()
        reg.maybe_auto_refresh(KEYS)
        time.sleep(0.2)
        assert reg.all_models() == before


class TestEnvOptOut:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_disable(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(NO_REFRESH_ENV, value)
        assert refresh_disabled_by_env() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_falsy_values_leave_enabled(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(NO_REFRESH_ENV, value)
        assert refresh_disabled_by_env() is False
