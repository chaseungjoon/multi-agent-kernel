"""The app offers a project ``.mak/config.yaml`` where there is none yet."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from cli.commands import handle_command
from cli.core.state import CliState
from cli.project_config import (
    create_project_config,
    needs_project_config,
    offer_project_config,
)
from rich.console import Console

from mak.config import (
    discover_config_path,
    load_config,
    packaged_config_path,
    user_config_dir,
)

_USER_CONFIG = """\
planner:
  model: "claude-opus-5"
agents:
  - type: "openai_api"
    model: "gpt-5"
"""


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=120, highlight=False), buffer


def _answer(reply: bool, asked: list[str]) -> object:
    def confirm(question: str) -> bool:
        asked.append(question)
        return reply

    return confirm


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


class TestOffer:
    def test_yes_creates_the_config_from_the_packaged_default(
        self, project: Path
    ) -> None:
        asked: list[str] = []
        console, _ = _console()
        state = CliState(work_dir=str(project))
        created = offer_project_config(state, console, _answer(True, asked))  # type: ignore[arg-type]
        assert created == project.resolve() / ".mak" / "config.yaml"
        assert created.read_text() == packaged_config_path().read_text()
        assert "MAK's default config" in asked[0]
        # Discovery now finds the project's own config first.
        assert discover_config_path(project) == project / ".mak" / "config.yaml"
        load_config(created)

    def test_the_users_config_is_the_seed_when_there_is_one(
        self, project: Path
    ) -> None:
        user = user_config_dir() / "config.yaml"
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(_USER_CONFIG)
        asked: list[str] = []
        console, _ = _console()
        state = CliState(work_dir=str(project))
        created = offer_project_config(state, console, _answer(True, asked))  # type: ignore[arg-type]
        assert created is not None
        assert created.read_text() == _USER_CONFIG
        assert str(user) in asked[0] or "~/" in asked[0]

    def test_no_writes_nothing(self, project: Path) -> None:
        console, buffer = _console()
        state = CliState(work_dir=str(project))
        assert offer_project_config(state, console, _answer(False, [])) is None  # type: ignore[arg-type]
        assert not (project / ".mak").exists()
        assert "created on the first run" in buffer.getvalue()

    def test_an_existing_mak_dir_is_not_asked_about(self, project: Path) -> None:
        (project / ".mak").mkdir()
        asked: list[str] = []
        console, _ = _console()
        offer_project_config(
            CliState(work_dir=str(project)), console, _answer(True, asked)  # type: ignore[arg-type]
        )
        assert asked == []
        assert not (project / ".mak" / "config.yaml").exists()

    def test_an_explicit_config_is_not_second_guessed(
        self, project: Path, tmp_path: Path
    ) -> None:
        asked: list[str] = []
        console, _ = _console()
        state = CliState(work_dir=str(project), config_path=str(tmp_path / "x.yaml"))
        offer_project_config(state, console, _answer(True, asked))  # type: ignore[arg-type]
        assert asked == []


class TestHelpers:
    def test_needs_a_config_only_without_a_mak_dir(self, project: Path) -> None:
        assert needs_project_config(project)
        (project / ".mak").mkdir()
        assert not needs_project_config(project)

    def test_an_existing_config_is_never_overwritten(self, project: Path) -> None:
        target = project / ".mak" / "config.yaml"
        target.parent.mkdir()
        target.write_text("mine")
        assert create_project_config(project, packaged_config_path()) == target
        assert target.read_text() == "mine"


class TestWorkDirCommand:
    def test_a_successful_switch_asks_the_app_to_offer(self, project: Path) -> None:
        console, _ = _console()
        state = CliState()
        assert handle_command(f"/work-dir {project}", state, console) == "work_dir"
        assert state.work_dir == str(project.resolve())

    def test_a_failed_switch_does_not(self, tmp_path: Path) -> None:
        console, _ = _console()
        line = f"/work-dir {tmp_path / 'nope'}"
        assert handle_command(line, CliState(), console) is None
