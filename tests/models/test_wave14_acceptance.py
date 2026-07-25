"""Wave 14 acceptance: the full seed → refresh → persist → degrade cycle.

Real ``ModelRegistry``, real manifest file, real curation, fake sources. This is
the test that would fail if the wave's contract broke, independent of the unit
tests above.
"""
from __future__ import annotations

import json
from pathlib import Path

from mak.models.manifest import SCHEMA_VERSION
from mak.models.providers import ModelFetchError
from mak.models.registry import ModelRegistry
from tests.models.test_refresh import FakeSource

KEYS = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "GEMINI_API_KEY": "g"}


def test_wave14_end_to_end(tmp_path: Path) -> None:
    path = tmp_path / "models.json"

    # ── 1. Cold start: the packaged seed is the floor. ────────────────────────
    cold = ModelRegistry(manifest_path_=path, sources=[])
    assert cold.find("claude-opus-5") is not None
    assert cold.last_refresh is None
    assert cold.recommended_planner("anthropic") == "claude-opus-5"

    # ── 2. Refresh: a new model ships, an old one retires, junk arrives. ──────
    anthropic = FakeSource(
        "anthropic",
        [
            "claude-opus-5",
            "claude-opus-6",              # brand new, uncurated
            "claude-sonnet-5",
            "claude-haiku-4-5",
            "claude-haiku-4-5-20251001",  # dated snapshot of the line above
            # note: claude-fable-5 / claude-opus-4-8 / claude-sonnet-4-6 absent
        ],
    )
    openai = FakeSource(
        "openai",
        ["gpt-5.6-sol", "dall-e-3", "text-embedding-3-large", "whisper-1"],
    )
    gemini = FakeSource("gemini", ["gemini-3.5-flash"])

    reg = ModelRegistry(
        manifest_path_=path, sources=[anthropic, openai, gemini]
    )
    report = reg.refresh_now(KEYS)
    assert report.ok is True

    ids = {m.model_id for m in reg.all_models()}

    # The new model is present and immediately usable...
    new = reg.find("claude-opus-6")
    assert new is not None
    assert new.planner_ok is True
    # ...but MAK never judges it: no star without a human curating it.
    assert new.recommended is False
    assert new.planner_recommended is False

    # Non-chat endpoints never surface.
    assert "dall-e-3" not in ids
    assert "text-embedding-3-large" not in ids
    assert "whisper-1" not in ids

    # The dated snapshot collapsed into its undated base.
    assert "claude-haiku-4-5" in ids
    assert "claude-haiku-4-5-20251001" not in ids

    # Curated judgment survived the refresh untouched.
    opus5 = reg.find("claude-opus-5")
    sonnet5 = reg.find("claude-sonnet-5")
    haiku = reg.find("claude-haiku-4-5")
    assert opus5 is not None and opus5.planner_recommended is True
    assert sonnet5 is not None and sonnet5.recommended is True
    assert haiku is not None and haiku.planner_ok is False

    # A seed model the provider no longer offers stays visible, flagged.
    fable = reg.find("claude-fable-5")
    assert fable is not None
    assert fable.retired is True
    # ...and is never what MAK auto-selects.
    assert reg.recommended_planner("anthropic") == "claude-opus-5"

    # ── 3. The manifest on disk is valid and complete. ────────────────────────
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["last_refresh"]
    for provider in ("anthropic", "openai", "gemini"):
        assert raw["providers"][provider]["fetched_at"]
    # Judgment is never persisted — it is re-joined from curation on load.
    persisted = raw["providers"]["anthropic"]["models"][0]
    assert "recommended" not in persisted
    assert "planner_ok" not in persisted

    # ── 4. A fresh process reads the refreshed catalog back. ──────────────────
    restarted = ModelRegistry(manifest_path_=path, sources=[])
    assert restarted.find("claude-opus-6") is not None
    assert restarted.last_refresh is not None
    restarted_fable = restarted.find("claude-fable-5")
    assert restarted_fable is not None and restarted_fable.retired is True

    # ── 5. Total network failure degrades to "keep what we had". ──────────────
    offline = ModelRegistry(
        manifest_path_=path,
        sources=[
            FakeSource("anthropic", raises=ModelFetchError("offline")),
            FakeSource("openai", raises=ModelFetchError("offline")),
            FakeSource("gemini", raises=ModelFetchError("offline")),
        ],
    )
    before = offline.all_models()
    stamp_before = offline.last_refresh

    failed = offline.refresh_now(KEYS)

    assert failed.ok is False
    assert offline.all_models() == before          # nothing lost
    assert offline.last_refresh == stamp_before    # still due, will retry


def test_config_yaml_is_never_written(tmp_path: Path) -> None:
    """The load-bearing invariant: the catalog never touches model *choice*."""
    from mak.config import packaged_config_path

    config = packaged_config_path()
    before = config.read_bytes()

    reg = ModelRegistry(
        manifest_path_=tmp_path / "models.json",
        sources=[FakeSource("anthropic", ["claude-opus-99"])],
    )
    reg.refresh_now(KEYS)

    assert config.read_bytes() == before
