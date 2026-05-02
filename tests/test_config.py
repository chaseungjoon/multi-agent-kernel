"""Tests for MAK configuration loading and validation."""

from pathlib import Path

import pytest

from mak.config import (
    AgentConfig,
    GitConfig,
    MakConfig,
    NodeStoreConfig,
    PlannerConfig,
    SessionConfig,
    load_config,
)
from mak.core.exceptions import ConfigError

_MINIMAL_YAML = """\
agents:
  - type: "test_agent"
"""

_FULL_YAML = """\
session:
  work_dir: "/tmp/work"
  mak_dir: ".custom_mak"
  max_concurrent_agents: 5

planner:
  model: "claude-opus-4"
  max_retries: 5
  temperature: 0.7

agents:
  - type: "claude_code"
    max_instances: 3
    timeout: 600
  - type: "codex"
    max_instances: 1
    timeout: 120

git:
  auto_commit: false
  auto_push: true
  commit_prefix: "[TEST]"

node_store:
  include_patterns:
    - "**/*.py"
    - "**/*.ts"
  exclude_patterns:
    - "**/dist/**"
"""


def test_load_bundled_config() -> None:
    """The bundled mak/config.yaml loads without error."""
    config_path = Path(__file__).resolve().parent.parent / "mak" / "config.yaml"
    cfg = load_config(config_path)

    assert len(cfg.agents) >= 1
    assert cfg.session.mak_dir == ".mak"


def test_load_full_yaml(tmp_path: Path) -> None:
    """All explicit values override defaults."""
    path = tmp_path / "config.yaml"
    path.write_text(_FULL_YAML)

    cfg = load_config(path)

    assert cfg.session == SessionConfig(
        work_dir="/tmp/work",
        mak_dir=".custom_mak",
        max_concurrent_agents=5,
    )
    assert cfg.planner == PlannerConfig(
        model="claude-opus-4",
        max_retries=5,
        temperature=0.7,
    )
    assert cfg.agents == (
        AgentConfig(type="claude_code", max_instances=3, timeout=600),
        AgentConfig(type="codex", max_instances=1, timeout=120),
    )
    assert cfg.git == GitConfig(
        auto_commit=False,
        auto_push=True,
        commit_prefix="[TEST]",
    )
    assert cfg.node_store == NodeStoreConfig(
        include_patterns=("**/*.py", "**/*.ts"),
        exclude_patterns=("**/dist/**",),
    )


def test_load_minimal_yaml_applies_defaults(tmp_path: Path) -> None:
    """Missing optional sections get default values."""
    path = tmp_path / "config.yaml"
    path.write_text(_MINIMAL_YAML)

    cfg = load_config(path)

    assert cfg.session == SessionConfig()
    assert cfg.planner == PlannerConfig()
    assert cfg.git == GitConfig()
    assert cfg.node_store == NodeStoreConfig()
    assert cfg.agents == (AgentConfig(type="test_agent"),)


def test_agent_defaults() -> None:
    """AgentConfig applies expected defaults."""
    agent = AgentConfig(type="test")
    assert agent.max_instances == 2
    assert agent.timeout == 300


def test_file_not_found_raises_config_error() -> None:
    """Missing config file raises ConfigError."""
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/path/config.yaml")


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    """Malformed YAML raises ConfigError."""
    path = tmp_path / "bad.yaml"
    path.write_text("agents:\n  - type: [invalid\n")

    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


def test_missing_agents_section_raises_config_error(tmp_path: Path) -> None:
    """Config without agents section raises ConfigError."""
    path = tmp_path / "config.yaml"
    path.write_text("session:\n  work_dir: '.'\n")

    with pytest.raises(ConfigError, match="'agents' section is required"):
        load_config(path)


def test_empty_agents_list_raises_config_error(tmp_path: Path) -> None:
    """Empty agents list raises ConfigError."""
    path = tmp_path / "config.yaml"
    path.write_text("agents: []\n")

    with pytest.raises(ConfigError, match="non-empty list"):
        load_config(path)


def test_agent_missing_type_raises_config_error(tmp_path: Path) -> None:
    """Agent entry without type raises ConfigError."""
    path = tmp_path / "config.yaml"
    path.write_text("agents:\n  - max_instances: 2\n")

    with pytest.raises(ConfigError, match="'type' field"):
        load_config(path)


def test_non_mapping_yaml_raises_config_error(tmp_path: Path) -> None:
    """YAML that parses to a non-mapping raises ConfigError."""
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a\n- list\n")

    with pytest.raises(ConfigError, match="YAML mapping"):
        load_config(path)


def test_frozen_dataclasses_are_immutable() -> None:
    """Config dataclasses reject attribute mutation."""
    cfg = MakConfig()
    with pytest.raises(AttributeError):
        cfg.session = SessionConfig(work_dir="/other")  # type: ignore[misc]

    agent = AgentConfig(type="test")
    with pytest.raises(AttributeError):
        agent.type = "other"  # type: ignore[misc]
