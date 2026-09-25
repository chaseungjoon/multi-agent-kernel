"""The same settings build the same run, whichever front end entered them (D25.5).

Each row states one logical setting twice: as ``mak run`` arguments and as the
interactive app's ``CliState``. Both go through their real front-end code —
``request_from_args`` + ``build_config`` with the process environment for
``mak run``; ``config_for`` + ``session_env`` for the app — and must agree on
the resulting ``MakConfig`` and the planner key.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from cli.core.state import CliState
from cli.runner import build_session, config_for, session_env

from mak.__main__ import parse_args, request_from_args
from mak.application import PlannerRoute, build_config, resolve_planner_key
from mak.config import MakConfig
from mak.endpoints.store import save_user_endpoints
from mak.endpoints.types import EndpointConfig, Location, Transport

_CONFIG = """\
planner:
  model: "claude-opus-5"
agents:
  - type: "anthropic_api"
    model: "claude-sonnet-5"
"""

_KEYS = {"ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oai", "NV_KEY": "sk-nv"}
_LOCAL = "http://localhost:11434"


def _nv_endpoint() -> EndpointConfig:
    return EndpointConfig(
        id="nv",
        transport=Transport.OPENAI_CHAT,
        base_url="https://nv.example/v1",
        api_key_env="NV_KEY",
        location=Location.HOSTED,
        display_name="nv",
    )


# name -> (extra ``mak run`` args, how the app is set up to mean the same)
_ROWS: dict[str, tuple[list[str], Callable[[CliState], None]]] = {
    "hosted planner": (
        [],
        lambda s: s.set_cloud_planner("anthropic", "claude-opus-5"),
    ),
    "endpoint planner": (
        [],
        lambda s: s.set_endpoint_planner("nv", "meta/llama"),
    ),
    "local planner": (
        [],
        lambda s: s.set_local_planner("ollama", "qwen2.5-coder:14b", _LOCAL),
    ),
    "gateway planner": (
        [],
        lambda s: setattr(
            s, "planner", PlannerRoute.hosted("openai", "gpt-5", "https://gw.example/v1")
        ),
    ),
    "roster with an endpoint": (
        ["--models", "nv:meta/llama", "anthropic:claude-opus-5"],
        lambda s: setattr(
            s, "selected_models", ["nv:meta/llama", "anthropic:claude-opus-5"]
        ),
    ),
    "max agents": (
        [],
        lambda s: setattr(s, "max_agents", 5),
    ),
}


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    project = (tmp_path / "project").resolve()
    (project / ".mak").mkdir(parents=True)
    (project / ".mak" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    save_user_endpoints((_nv_endpoint(),))
    return project


def _run_side(
    state: CliState, work_dir: Path, extra: list[str], monkeypatch: pytest.MonkeyPatch
) -> tuple[MakConfig, str | None]:
    """Build what ``mak run`` would for the same settings, keys exported."""
    for name, value in _KEYS.items():
        monkeypatch.setenv(name, value)
    argv = [
        "--task", "t",
        "--work-dir", str(work_dir),
        "--planner", state.planner.spec(),
        "--max-agents", str(state.max_agents),
        *extra,
    ]
    config = build_config(request_from_args(parse_args(argv)))
    return config, resolve_planner_key(config)


def _app_side(state: CliState) -> tuple[MakConfig, str | None]:
    """Build what the interactive app would, keys held only in the session."""
    return config_for(state), resolve_planner_key(config_for(state), session_env(state))


@pytest.mark.parametrize("row", list(_ROWS))
def test_both_front_ends_build_the_same_run(
    row: str, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra, arrange = _ROWS[row]
    for name in _KEYS:
        monkeypatch.delenv(name, raising=False)
    state = CliState(work_dir=str(work_dir), api_keys=dict(_KEYS))
    arrange(state)

    app_config, app_key = _app_side(state)
    run_config, run_key = _run_side(state, work_dir, extra, monkeypatch)

    assert app_config == run_config
    assert app_key == run_key


def test_the_keys_resolve_as_expected(work_dir: Path) -> None:
    """The parity rows are not vacuous: each planner gets its own credential."""
    expected = {
        "hosted planner": "sk-ant",
        "endpoint planner": "sk-nv",
        "local planner": None,
        "gateway planner": None,
    }
    for row, key in expected.items():
        state = CliState(work_dir=str(work_dir), api_keys=dict(_KEYS))
        _ROWS[row][1](state)
        assert _app_side(state)[1] == key, row


def test_the_app_never_mutates_os_environ(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _KEYS:
        monkeypatch.delenv(name, raising=False)
    state = CliState(
        work_dir=str(work_dir),
        api_keys={"ANTHROPIC_API_KEY": "sk-session-only"},
        selected_models=["anthropic:claude-opus-5"],
    )
    before = dict(os.environ)
    session = build_session("t", state)
    assert dict(os.environ) == before
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert session is not None
