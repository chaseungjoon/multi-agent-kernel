"""A failing slash command costs that command, not the session (D25.9)."""
from __future__ import annotations

import io
import logging
from pathlib import Path

import cli.app as app_mod
import pytest
from cli.app import MakCli
from cli.core.state import CliState
from rich.console import Console


class _Prompt:
    """A prompt session that replays lines, then ends the loop with Ctrl+D."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def prompt(self, *_args: object, **_kwargs: object) -> str:
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


def _app(tmp_path: Path, lines: list[str]) -> tuple[MakCli, io.StringIO]:
    """Build the app around a scripted prompt, skipping terminal setup."""
    (tmp_path / ".mak").mkdir(exist_ok=True)  # nothing to offer at startup
    buffer = io.StringIO()
    app = MakCli.__new__(MakCli)
    app.console = Console(file=buffer, width=120, highlight=False)
    app.state = CliState(
        work_dir=str(tmp_path), api_keys={"ANTHROPIC_API_KEY": "sk"}
    )
    app._session_tokens = 0
    app._prompt_session = _Prompt(lines)  # type: ignore[assignment]
    return app, buffer


def _boom(text: str, _state: CliState, _console: Console) -> str | None:
    if text.startswith("/boom"):
        raise RuntimeError("handler exploded")
    return None


def test_a_raising_command_prints_one_line_and_the_loop_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(app_mod, "handle_command", _boom)
    app, buffer = _app(tmp_path, ["/boom now", "/status"])
    with caplog.at_level(logging.DEBUG, logger="cli.app"):
        app.run()
    out = buffer.getvalue()
    assert "/boom failed: RuntimeError: handler exploded" in out
    assert "Traceback" not in out
    # The loop kept going after the failure, to a normal session end.
    assert "Session ended" in out
    record = next(r for r in caplog.records if "/boom" in r.getMessage())
    assert record.levelno == logging.DEBUG
    assert record.exc_info is not None


def test_the_session_state_survives_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_mod, "handle_command", _boom)
    app, _buffer = _app(tmp_path, [])
    app.state.max_agents = 7
    assert app._dispatch_command("/boom") is None
    assert app.state.max_agents == 7


@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_ctrl_c_and_ctrl_d_are_not_swallowed(
    exc: type[BaseException], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_it(*_args: object) -> str | None:
        raise exc

    monkeypatch.setattr(app_mod, "handle_command", raise_it)
    app, _buffer = _app(tmp_path, [])
    with pytest.raises(exc):
        app._dispatch_command("/anything")


def test_ctrl_d_at_the_prompt_still_exits(tmp_path: Path) -> None:
    app, buffer = _app(tmp_path, [])
    app.run()
    assert "Session ended" in buffer.getvalue()


def test_the_app_offers_a_project_config_at_start_and_after_work_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    offered: list[str] = []
    monkeypatch.setattr(
        app_mod,
        "offer_project_config",
        lambda state, _console: offered.append(state.work_dir),
    )
    app, _buffer = _app(tmp_path, [f"/work-dir {other}"])
    app.run()
    assert offered == [str(tmp_path), str(other.resolve())]
