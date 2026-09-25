"""The app shows and runs with the config file's planner and agents until changed.

Regression: a project whose ``.mak/config.yaml`` named ``claude-opus-5-5`` for
the planner and the agent showed — and ran — ``anthropic:claude-opus-5``, the
catalog's recommendation, because the app never read either section.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from cli.commands import handle_command
from cli.config_sync import agent_label, sync_with_config
from cli.core.state import CliState
from cli.runner import config_for
from rich.console import Console

from mak.application import PlannerRoute
from mak.config import AgentConfig, PlannerConfig

_PROJECT_CONFIG = """\
planner:
  model: "claude-opus-5-5"
  max_retries: 3
agents:
  - type: "anthropic_api"
    model: "claude-opus-5-5"
    api_key_env: "ANTHROPIC_API_KEY"
    max_instances: 2
    timeout: 300
"""

_OTHER_CONFIG = """\
planner:
  model: "qwen2.5-coder:14b"
  backend: "ollama"
  base_url: "http://localhost:11434"
agents:
  - type: "ollama_api"
    model: "qwen2.5-coder:14b"
    base_url: "http://localhost:11434"
"""


def _project(root: Path, body: str) -> Path:
    (root / ".mak").mkdir(parents=True)
    (root / ".mak" / "config.yaml").write_text(body, encoding="utf-8")
    return root


def _console() -> Console:
    return Console(file=io.StringIO(), width=200)


@pytest.fixture
def state(tmp_path: Path) -> CliState:
    project = _project(tmp_path / "imposter", _PROJECT_CONFIG)
    state = CliState(work_dir=str(project), api_keys={"ANTHROPIC_API_KEY": "sk"})
    sync_with_config(state)
    return state


class TestStartup:
    def test_the_configs_planner_and_agents_are_shown(self, state: CliState) -> None:
        assert state.planner_spec() == "anthropic:claude-opus-5-5"
        assert state.models_display() == "anthropic:claude-opus-5-5"
        assert not state.planner_pinned
        assert state.selected_models == []

    def test_a_run_uses_the_config_sections_verbatim(self, state: CliState) -> None:
        config = config_for(state)
        assert config.planner.model == "claude-opus-5-5"
        assert config.planner.max_retries == 3
        (agent,) = config.agents
        assert (agent.model, agent.max_instances, agent.timeout) == (
            "claude-opus-5-5", 2, 300,
        )

    def test_a_config_planner_setting_the_route_cannot_hold_survives(
        self, tmp_path: Path
    ) -> None:
        body = _PROJECT_CONFIG.replace(
            '  max_retries: 3\n', '  max_retries: 3\n  api_key_env: "VLLM_TOKEN"\n'
        )
        state = CliState(work_dir=str(_project(tmp_path / "p", body)))
        sync_with_config(state)
        assert config_for(state).planner.api_key_env == "VLLM_TOKEN"


class TestChoosing:
    def test_a_chosen_planner_overrides_the_config(self, state: CliState) -> None:
        state.set_cloud_planner("anthropic", "claude-sonnet-5")
        assert config_for(state).planner.model == "claude-sonnet-5"

    def test_a_chosen_roster_overrides_the_config(self, state: CliState) -> None:
        state.selected_models = ["anthropic:claude-sonnet-5"]
        assert [a.model for a in config_for(state).agents] == ["claude-sonnet-5"]
        assert state.models_display() == "anthropic:claude-sonnet-5"


class TestWorkDirAndConfig:
    def test_an_unpinned_planner_follows_the_new_work_dirs_config(
        self, state: CliState, tmp_path: Path
    ) -> None:
        other = _project(tmp_path / "other", _OTHER_CONFIG)
        handle_command(f"/work-dir {other}", state, _console())
        assert state.planner == PlannerRoute.local(
            "ollama", "qwen2.5-coder:14b", "http://localhost:11434"
        )
        assert state.models_display() == (
            "ollama:qwen2.5-coder:14b@http://localhost:11434"
        )

    def test_a_pinned_planner_survives_a_work_dir_change(
        self, state: CliState, tmp_path: Path
    ) -> None:
        state.set_cloud_planner("anthropic", "claude-sonnet-5")
        other = _project(tmp_path / "other", _OTHER_CONFIG)
        handle_command(f"/work-dir {other}", state, _console())
        assert state.planner_spec() == "anthropic:claude-sonnet-5"

    def test_an_explicit_config_is_read(self, state: CliState, tmp_path: Path) -> None:
        other = _project(tmp_path / "other", _OTHER_CONFIG) / ".mak" / "config.yaml"
        handle_command(f"/config {other}", state, _console())
        assert state.planner.kind == "local"


class TestFromPlannerConfig:
    @pytest.mark.parametrize(
        ("planner", "spec"),
        [
            (PlannerConfig(model="claude-opus-5-5"), "anthropic:claude-opus-5-5"),
            (PlannerConfig(model="gpt-5", backend="openai"), "openai:gpt-5"),
            (PlannerConfig(model="m", endpoint="openrouter"), "openrouter:m"),
            (
                PlannerConfig(model="q", backend="ollama", base_url="http://h:1"),
                "ollama:q@http://h:1",
            ),
            (
                PlannerConfig(model="q", backend="openai", base_url="http://h:1/v1"),
                "local:q@http://h:1/v1",
            ),
        ],
    )
    def test_each_route_is_read(self, planner: PlannerConfig, spec: str) -> None:
        route = PlannerRoute.from_planner_config(planner)
        assert route is not None
        assert route.spec() == spec

    @pytest.mark.parametrize(
        "planner",
        [
            PlannerConfig(model=""),
            PlannerConfig(model="mystery-model"),
            PlannerConfig(model="c", backend="anthropic", base_url="http://h:1"),
        ],
    )
    def test_an_unrepresentable_planner_is_none(self, planner: PlannerConfig) -> None:
        assert PlannerRoute.from_planner_config(planner) is None

    def test_an_unrepresentable_planner_pins_the_default(self, tmp_path: Path) -> None:
        body = _PROJECT_CONFIG.replace('"claude-opus-5-5"\n  max', '"mystery"\n  max')
        state = CliState(work_dir=str(_project(tmp_path / "p", body)))
        sync_with_config(state)
        assert state.planner_pinned
        assert state.planner.kind == "hosted"


def test_agent_labels_follow_the_models_grammar() -> None:
    assert agent_label(AgentConfig(type="anthropic_api", model="m")) == "anthropic:m"
    assert agent_label(AgentConfig(type="openai_api")) == "openai"
    assert agent_label(AgentConfig(type="claude_code")) == "claude_code"
    assert agent_label(
        AgentConfig(type="", endpoint="openrouter", model="x/y")
    ) == "openrouter:x/y"
