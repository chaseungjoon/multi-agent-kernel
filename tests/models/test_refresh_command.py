"""``/refresh-models`` behaviour, driven through the real command dispatcher."""
from __future__ import annotations

import io
from pathlib import Path

import cli.commands as commands
import pytest
from cli.completer import COMMANDS
from cli.core.state import CliState
from rich.console import Console

from mak.models.providers import ModelFetchError
from mak.models.registry import ModelRegistry
from tests.models.test_refresh import FakeSource

KEYS = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "GEMINI_API_KEY": "g"}


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=100, force_terminal=False, no_color=True), buf


def _use_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sources: list[FakeSource]
) -> ModelRegistry:
    reg = ModelRegistry(manifest_path_=tmp_path / "models.json", sources=sources)
    monkeypatch.setattr(commands, "registry", lambda: reg)
    monkeypatch.setattr(commands, "all_models", reg.all_models)
    return reg


class TestCommandRegistration:
    def test_listed_in_completions(self) -> None:
        assert any(name == "/refresh-models" for name, _ in COMMANDS)

    def test_dispatches_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_registry(monkeypatch, tmp_path, [FakeSource("openai", ["gpt-9"])])
        console, _buf = _console()
        state = CliState(api_keys=dict(KEYS))
        assert commands.handle_command("/refresh-models", state, console) is None


class TestOutput:
    def test_reports_added_on_first_refresh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        reg = _use_registry(
            monkeypatch, tmp_path, [FakeSource("anthropic", ["claude-opus-5"])]
        )
        console, buf = _console()
        commands.handle_command(
            "/refresh-models", CliState(api_keys=dict(KEYS)), console
        )
        out = buf.getvalue()
        assert "+ claude-opus-5" in out
        assert "refreshed" in out.lower()
        assert reg.find("claude-opus-5") is not None

    def test_reports_removed_against_previous_fetch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """"Removed" is relative to the last *fetch*, not to the seed."""
        source = FakeSource("anthropic", ["claude-opus-5", "claude-opus-4-8"])
        reg = _use_registry(monkeypatch, tmp_path, [source])
        reg.refresh_now(KEYS)  # baseline: two models

        source._models = ["claude-opus-5"]  # provider drops one
        console, buf = _console()
        commands.handle_command(
            "/refresh-models", CliState(api_keys=dict(KEYS)), console
        )
        assert "- claude-opus-4-8" in buf.getvalue()

    def test_provider_error_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_registry(
            monkeypatch,
            tmp_path,
            [FakeSource("anthropic", raises=ModelFetchError("offline"))],
        )
        console, buf = _console()
        commands.handle_command(
            "/refresh-models", CliState(api_keys=dict(KEYS)), console
        )
        out = buf.getvalue()
        assert "keeping cached list" in out

    def test_missing_key_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_registry(monkeypatch, tmp_path, [FakeSource("openai", ["gpt-9"])])
        console, buf = _console()
        state = CliState(api_keys={"OPENAI_API_KEY": ""})
        commands.handle_command("/refresh-models", state, console)
        assert "no API key" in buf.getvalue()

    def test_no_changes_message(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        source = FakeSource("openai", ["gpt-5.6-sol", "gpt-5.6-terra",
                                       "gpt-5.5", "gpt-5.6-luna"])
        reg = _use_registry(monkeypatch, tmp_path, [source])
        reg.refresh_now(KEYS)  # first pass establishes the baseline
        console, buf = _console()
        commands.handle_command(
            "/refresh-models", CliState(api_keys=dict(KEYS)), console
        )
        assert "up to date" in buf.getvalue()


class TestRetiredSelectionWarnings:
    def test_warns_when_active_planner_retired_but_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_registry(
            monkeypatch, tmp_path, [FakeSource("anthropic", ["claude-opus-5"])]
        )
        console, buf = _console()
        state = CliState(api_keys=dict(KEYS), planner_model="claude-haiku-4-5")
        commands.handle_command("/refresh-models", state, console)

        assert "no longer offered" in buf.getvalue()
        # MAK never re-picks a model for the user.
        assert state.planner_model == "claude-haiku-4-5"

    def test_warns_when_active_agent_model_retired(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_registry(
            monkeypatch, tmp_path, [FakeSource("anthropic", ["claude-opus-5"])]
        )
        console, buf = _console()
        state = CliState(
            api_keys=dict(KEYS),
            selected_models=["anthropic:claude-sonnet-4-6"],
        )
        commands.handle_command("/refresh-models", state, console)

        assert "no longer offered" in buf.getvalue()
        assert state.selected_models == ["anthropic:claude-sonnet-4-6"]

    def test_no_warning_when_selections_are_live(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _use_registry(
            monkeypatch, tmp_path, [FakeSource("anthropic", ["claude-opus-5"])]
        )
        console, buf = _console()
        state = CliState(
            api_keys=dict(KEYS),
            planner_model="claude-opus-5",
            selected_models=["anthropic:claude-opus-5"],
        )
        commands.handle_command("/refresh-models", state, console)
        assert "no longer offered" not in buf.getvalue()
