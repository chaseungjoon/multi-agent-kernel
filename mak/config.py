"""Configuration loading and validation for MAK."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mak.core.exceptions import ConfigError

_DEFAULT_INCLUDE: list[str] = ["**/*.py"]
_DEFAULT_EXCLUDE: list[str] = [
    "**/node_modules/**",
    "**/.venv/**",
    "**/__pycache__/**",
]


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Configuration for a single agent type."""

    type: str
    max_instances: int = 2
    timeout: int = 300


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Session-level configuration."""

    work_dir: str = "."
    mak_dir: str = ".mak"
    max_concurrent_agents: int = 3


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """Planner model configuration."""

    model: str = "claude-sonnet-4-6"
    max_retries: int = 3
    temperature: float = 0.0


@dataclass(frozen=True, slots=True)
class GitConfig:
    """Git integration configuration."""

    auto_commit: bool = True
    auto_push: bool = False
    commit_prefix: str = "[MAK]"


@dataclass(frozen=True, slots=True)
class NodeStoreConfig:
    """Node store file-matching configuration."""

    include_patterns: tuple[str, ...] = tuple(_DEFAULT_INCLUDE)
    exclude_patterns: tuple[str, ...] = tuple(_DEFAULT_EXCLUDE)


@dataclass(frozen=True, slots=True)
class MakConfig:
    """Top-level MAK configuration."""

    session: SessionConfig = field(default_factory=SessionConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    agents: tuple[AgentConfig, ...] = (
        AgentConfig(type="claude_code"),
        AgentConfig(type="codex", max_instances=1),
    )
    git: GitConfig = field(default_factory=GitConfig)
    node_store: NodeStoreConfig = field(default_factory=NodeStoreConfig)


def _parse_agent(raw: dict[str, Any]) -> AgentConfig:
    if "type" not in raw:
        raise ConfigError("each agent entry must have a 'type' field")
    return AgentConfig(
        type=raw["type"],
        max_instances=int(raw.get("max_instances", 2)),
        timeout=int(raw.get("timeout", 300)),
    )


def _parse_session(raw: dict[str, Any]) -> SessionConfig:
    return SessionConfig(
        work_dir=str(raw.get("work_dir", ".")),
        mak_dir=str(raw.get("mak_dir", ".mak")),
        max_concurrent_agents=int(raw.get("max_concurrent_agents", 3)),
    )


def _parse_planner(raw: dict[str, Any]) -> PlannerConfig:
    return PlannerConfig(
        model=str(raw.get("model", "claude-sonnet-4-6")),
        max_retries=int(raw.get("max_retries", 3)),
        temperature=float(raw.get("temperature", 0.0)),
    )


def _parse_git(raw: dict[str, Any]) -> GitConfig:
    return GitConfig(
        auto_commit=bool(raw.get("auto_commit", True)),
        auto_push=bool(raw.get("auto_push", False)),
        commit_prefix=str(raw.get("commit_prefix", "[MAK]")),
    )


def _parse_node_store(raw: dict[str, Any]) -> NodeStoreConfig:
    include = raw.get("include_patterns", _DEFAULT_INCLUDE)
    exclude = raw.get("exclude_patterns", _DEFAULT_EXCLUDE)
    return NodeStoreConfig(
        include_patterns=tuple(str(p) for p in include),
        exclude_patterns=tuple(str(p) for p in exclude),
    )


def load_config(path: Path | str) -> MakConfig:
    """Load and validate a MAK configuration file."""
    config_path = Path(path)

    if not config_path.exists():
        raise ConfigError(f"configuration file not found: {config_path}")

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in configuration file: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("configuration file must contain a YAML mapping")

    raw_agents = data.get("agents")
    if raw_agents is not None:
        if not isinstance(raw_agents, list) or len(raw_agents) == 0:
            raise ConfigError("'agents' must be a non-empty list")
        agents = tuple(_parse_agent(a) for a in raw_agents)
    else:
        raise ConfigError("'agents' section is required with at least one entry")

    return MakConfig(
        session=_parse_session(data.get("session", {})),
        planner=_parse_planner(data.get("planner", {})),
        agents=agents,
        git=_parse_git(data.get("git", {})),
        node_store=_parse_node_store(data.get("node_store", {})),
    )
