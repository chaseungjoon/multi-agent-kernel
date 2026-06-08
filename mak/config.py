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

_TRUE_STRINGS = {"true", "1", "yes", "on"}
_FALSE_STRINGS = {"false", "0", "no", "off"}


def _as_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"'{key}' must be an integer, got {value!r}") from exc


def _as_float(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' must be a number, got {value!r}") from exc


def _opt_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return None if value is None else str(value)


def _as_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    raise ConfigError(f"'{key}' must be a boolean, got {value!r}")


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Configuration for a single agent type.

    ``model`` / ``api_key_env`` parameterize API adapters (the env var is read at
    composition time so a key is never persisted in config). ``cmd`` points a CLI
    adapter at its binary. All three are optional.
    """

    type: str
    max_instances: int = 2
    timeout: int = 300
    model: str | None = None
    api_key_env: str | None = None
    cmd: str | None = None


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Session-level configuration."""

    work_dir: str = "."
    mak_dir: str = ".mak"
    max_concurrent_agents: int = 3
    lock_timeout_s: float = 300.0
    deadlock_check_interval_s: float = 5.0


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
        AgentConfig(type="anthropic_api"),
        AgentConfig(type="openai_api"),
        AgentConfig(type="gemini_api"),
    )
    git: GitConfig = field(default_factory=GitConfig)
    node_store: NodeStoreConfig = field(default_factory=NodeStoreConfig)


def _parse_agent(raw: dict[str, Any]) -> AgentConfig:
    if "type" not in raw:
        raise ConfigError("each agent entry must have a 'type' field")
    return AgentConfig(
        type=str(raw["type"]),
        max_instances=_as_int(raw, "max_instances", 2),
        timeout=_as_int(raw, "timeout", 300),
        model=_opt_str(raw, "model"),
        api_key_env=_opt_str(raw, "api_key_env"),
        cmd=_opt_str(raw, "cmd"),
    )


def _parse_session(raw: dict[str, Any]) -> SessionConfig:
    return SessionConfig(
        work_dir=str(raw.get("work_dir", ".")),
        mak_dir=str(raw.get("mak_dir", ".mak")),
        max_concurrent_agents=_as_int(raw, "max_concurrent_agents", 3),
        lock_timeout_s=_as_float(raw, "lock_timeout_s", 300.0),
        deadlock_check_interval_s=_as_float(
            raw, "deadlock_check_interval_s", 5.0),
    )


def _parse_planner(raw: dict[str, Any]) -> PlannerConfig:
    return PlannerConfig(
        model=str(raw.get("model", "claude-sonnet-4-6")),
        max_retries=_as_int(raw, "max_retries", 3),
        temperature=_as_float(raw, "temperature", 0.0),
    )


def _parse_git(raw: dict[str, Any]) -> GitConfig:
    return GitConfig(
        auto_commit=_as_bool(raw, "auto_commit", True),
        auto_push=_as_bool(raw, "auto_push", False),
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
        raise ConfigError(
            f"invalid YAML in configuration file: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("configuration file must contain a YAML mapping")

    raw_agents = data.get("agents")
    if raw_agents is not None:
        if not isinstance(raw_agents, list) or len(raw_agents) == 0:
            raise ConfigError("'agents' must be a non-empty list")
        agents = tuple(_parse_agent(a) for a in raw_agents)
    else:
        raise ConfigError(
            "'agents' section is required with at least one entry")

    return MakConfig(
        session=_parse_session(data.get("session", {})),
        planner=_parse_planner(data.get("planner", {})),
        agents=agents,
        git=_parse_git(data.get("git", {})),
        node_store=_parse_node_store(data.get("node_store", {})),
    )
