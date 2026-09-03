"""Tests for local runtime discovery and the curated suggestion table."""

from __future__ import annotations

import time

import pytest

from mak.local.discovery import LOCAL_BASE_URL_ENV, discover, scan_targets
from mak.local.ollama_client import DEFAULT_BASE_URL
from mak.local.recommended import (
    RECOMMENDED,
    default_suggestion,
    recommended_for,
)
from mak.local.runtime import (
    KIND_OLLAMA,
    KIND_OPENAI_COMPATIBLE,
    LocalRuntime,
)

_LM_STUDIO = "http://localhost:1234/v1"


def _ollama(url: str = DEFAULT_BASE_URL) -> LocalRuntime:
    return LocalRuntime(
        kind=KIND_OLLAMA,
        name="Ollama",
        base_url=url,
        version="0.5.7",
        models=("qwen2.5-coder:14b", "llama3.1:8b"),
    )


def _lm_studio(url: str = _LM_STUDIO) -> LocalRuntime:
    return LocalRuntime(
        kind=KIND_OPENAI_COMPATIBLE, name="LM Studio", base_url=url, models=("m",)
    )


class TestDiscover:
    def test_reports_every_runtime_that_answered_ollama_first(self) -> None:
        def prober(url: str, _timeout: float) -> LocalRuntime | None:
            if url == DEFAULT_BASE_URL:
                return _ollama()
            if url == _LM_STUDIO:
                return _lm_studio()
            return None

        found = discover(prober=prober)
        assert [r.name for r in found] == ["Ollama", "LM Studio"]
        assert found[0].kind == KIND_OLLAMA
        assert found[0].models == ("qwen2.5-coder:14b", "llama3.1:8b")

    def test_nothing_running_returns_empty(self) -> None:
        assert discover(prober=lambda _url, _t: None) == []

    def test_a_hung_endpoint_does_not_extend_the_scan(self) -> None:
        # Probes run concurrently: one slow address must cost the timeout once,
        # not once per address ahead of it.
        def prober(url: str, timeout: float) -> LocalRuntime | None:
            if url == DEFAULT_BASE_URL:
                time.sleep(timeout)
                return None
            return _lm_studio() if url == _LM_STUDIO else None

        start = time.monotonic()
        found = discover(prober=prober, timeout=0.3)
        elapsed = time.monotonic() - start
        assert [r.name for r in found] == ["LM Studio"]
        assert elapsed < 0.3 * len(scan_targets())

    def test_a_raising_prober_cannot_fail_discovery(self) -> None:
        def prober(url: str, _timeout: float) -> LocalRuntime | None:
            if url == DEFAULT_BASE_URL:
                raise RuntimeError("boom")
            return _lm_studio() if url == _LM_STUDIO else None

        assert [r.name for r in discover(prober=prober)] == ["LM Studio"]

    def test_duplicate_base_urls_are_reported_once(self) -> None:
        assert len(discover(prober=lambda _url, _t: _ollama())) == 1


class TestScanTargets:
    def test_the_well_known_ports_are_scanned_ollama_first(self) -> None:
        targets = scan_targets()
        assert targets[0] == DEFAULT_BASE_URL
        assert _LM_STUDIO in targets
        assert "http://localhost:8000/v1" in targets
        assert "http://localhost:8080/v1" in targets

    def test_the_env_var_is_included(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LOCAL_BASE_URL_ENV, "http://gpu-box:9000/v1")
        assert "http://gpu-box:9000/v1" in scan_targets()

    def test_the_env_var_is_deduplicated_against_a_well_known_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(LOCAL_BASE_URL_ENV, f"{DEFAULT_BASE_URL}/")
        assert scan_targets().count(DEFAULT_BASE_URL) == 1

    def test_extra_urls_are_appended(self) -> None:
        assert scan_targets(["http://other:1/v1"])[-1] == "http://other:1/v1"


class TestRecommended:
    def test_entries_are_unique_exact_tags(self) -> None:
        tags = [entry.tag for entry in RECOMMENDED]
        assert len(tags) == len(set(tags))
        # An exact Ollama tag: a suggestion that cannot be pasted into
        # `ollama pull` is not a suggestion.
        assert all(":" in tag and " " not in tag for tag in tags)

    def test_the_table_is_ordered_smallest_first(self) -> None:
        sizes = [entry.params_b for entry in RECOMMENDED]
        assert sizes == sorted(sizes)

    def test_default_suggestion_is_the_smallest(self) -> None:
        assert default_suggestion() is RECOMMENDED[0]

    def test_lookup_by_exact_tag(self) -> None:
        entry = recommended_for("qwen2.5-coder:14b")
        assert entry is not None and entry.params_b == pytest.approx(14.8)

    def test_an_unlisted_tag_has_no_opinion(self) -> None:
        assert recommended_for("some-other-model:3b") is None

    def test_small_models_are_flagged_for_a_hybrid_planner(self) -> None:
        small = recommended_for("qwen2.5-coder:7b")
        large = recommended_for("qwen2.5-coder:32b")
        assert small is not None and small.is_small() is True
        assert large is not None and large.is_small() is False
