"""Tests for mak.core.budget: catalog-driven output-token budgets."""

from __future__ import annotations

from typing import Any

import pytest

import mak.core.budget as budget_module
from mak.agent_runner.adapters.budget import resolve_agent_max_tokens
from mak.core.budget import resolve_output_budget


def _limits(monkeypatch: pytest.MonkeyPatch, limits: dict[str, int]) -> None:
    monkeypatch.setattr(budget_module, "documented_output_limits", lambda: limits)


class TestResolveOutputBudget:
    def test_uses_the_documented_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _limits(monkeypatch, {"m": 20000})
        assert resolve_output_budget(
            "m", fallback=16384, minimum=8192, maximum=32000
        ) == 20000

    def test_clamps_to_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _limits(monkeypatch, {"m": 128000})
        assert resolve_output_budget(
            "m", fallback=16384, minimum=8192, maximum=32000
        ) == 32000

    def test_raises_a_small_limit_to_the_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _limits(monkeypatch, {"m": 1024})
        assert resolve_output_budget(
            "m", fallback=16384, minimum=8192, maximum=32000
        ) == 8192

    def test_unknown_model_gets_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _limits(monkeypatch, {})
        assert resolve_output_budget(
            "shipped-tomorrow", fallback=16384, minimum=8192, maximum=32000
        ) == 16384

    def test_an_unreadable_catalog_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The catalog is an optimisation; losing it must not stop a run."""
        import mak.models.registry as registry_module

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("no manifest")

        monkeypatch.setattr(registry_module, "ModelRegistry", boom)
        budget_module.documented_output_limits.cache_clear()
        try:
            assert resolve_output_budget(
                "claude-opus-5", fallback=16384, minimum=8192, maximum=32000
            ) == 16384
        finally:
            budget_module.documented_output_limits.cache_clear()


class TestAgentBudget:
    def test_the_agent_budget_beats_the_old_constant(self) -> None:
        """12.5c: the regression guard.

        8192 was 6% of the documented limit, and just under the size of the
        whole-file rewrites this project emits — which is why the truncation
        presented as flaky rather than broken.
        """
        for model in ("claude-sonnet-5", "claude-opus-5"):
            assert resolve_agent_max_tokens(model) > 8192

    def test_an_unknown_model_still_gets_a_workable_budget(self) -> None:
        assert resolve_agent_max_tokens("shipped-tomorrow") >= 8192
