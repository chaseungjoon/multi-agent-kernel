"""Configuration loading, discovery, and validation for MAK."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mak.core.exceptions import ConfigError

_DEFAULT_INCLUDE: list[str] = ["**/*.py"]
# Directories that are never project source. ``.mak`` heads the list: the node
# store persists fragments as ``.py`` files under ``.mak/node_store/``, and
# ``Path.glob("**/*.py")`` descends into dotted directories, so without it every
# run re-ingests the previous run's output as if it were source (Wave 11). The
# session also skips the mak dir unconditionally — a user config that overrides
# ``exclude_patterns`` must not be able to switch that protection off.
_DEFAULT_EXCLUDE: list[str] = [
    "**/.mak/**",
    "**/node_modules/**",
    "**/.venv/**",
    "**/__pycache__/**",
    "**/.git/**",
    "**/build/**",
    "**/dist/**",
    "**/.tox/**",
    "**/.mypy_cache/**",
    "**/.pytest_cache/**",
    "**/site-packages/**",
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


def _opt_positive_int(raw: dict[str, Any], key: str) -> int | None:
    """Return an optional positive integer setting, or None when unset."""
    value = raw.get(key)
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' must be an integer, got {value!r}") from exc
    if number <= 0:
        raise ConfigError(f"'{key}' must be greater than 0, got {number}")
    return number


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
    adapter at its binary. All are optional.

    ``max_tokens`` caps the agent's output budget. ``None`` (the default) means
    "resolve it from the model catalog" for Anthropic and "send no cap, inherit
    the model's own maximum" for OpenAI/Gemini — a hardcoded constant here is
    what silently clipped whole-file rewrites. Set it to bound spend on a metered
    model, or to fit a local model whose real limit the catalog does not know.
    """

    type: str
    max_instances: int = 2
    timeout: int = 300
    model: str | None = None
    api_key_env: str | None = None
    cmd: str | None = None
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Session-level configuration.

    ``test_command`` is the shell command MAK runs in the work dir during teardown
    to gate an ``auto_push`` (and to report a pass/fail after a run). ``None``
    (the default) skips the test step entirely — nothing is run and teardown
    reports success. Set e.g. ``"pytest -q"`` to make the gate real.
    """

    work_dir: str = "."
    mak_dir: str = ".mak"
    max_concurrent_agents: int = 3
    lock_timeout_s: float = 300.0
    deadlock_check_interval_s: float = 5.0
    test_command: str | None = None


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """Planner model + quality configuration.

    No sampling knob: MAK's current default models (Claude Sonnet 5 / Opus 5 /
    Opus 4.8 / Fable 5, GPT-5.x, Gemini 3.x) reject ``temperature``/``top_p``,
    and the agent adapters already omit them. Steering is via the prompt, not
    sampling params.

    ``model`` is the planner's model id. It carries no hardcoded default — the empty
    string means "unset", to be supplied by ``config.yaml`` (the single source of
    truth for model choices, mirroring ``AgentConfig.model``). Every real run loads a
    ``config.yaml`` that names it; the CLI only writes it back when the user changes it.

    ``validate`` (default on) runs deterministic plan validation against the code
    dependency graph after decomposition — grounding node ids and adding missing
    dependency edges (see ``mak.planner.validation``). ``strategy`` is ``oneshot``
    (single decomposition call) or ``outline`` (outline → per-step detail).
    ``self_critique`` adds one LLM reflection pass over a produced plan.
    """

    model: str = ""
    max_retries: int = 3
    validate: bool = True
    strategy: str = "oneshot"
    self_critique: bool = False


@dataclass(frozen=True, slots=True)
class ModelsConfig:
    """Provider model-catalog refresh policy.

    This governs the *catalog* — which models MAK offers to choose from — and
    never the *choice*: ``config.yaml`` remains the sole source of truth for the
    planner and agent models, and a catalog refresh never writes to it.

    ``auto_refresh`` re-fetches each provider's model list on the 1st and 15th
    (in the background, on startup). The ``MAK_NO_MODEL_REFRESH`` environment
    variable disables it independently.
    """

    auto_refresh: bool = True


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
    models: ModelsConfig = field(default_factory=ModelsConfig)


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
        max_tokens=_opt_positive_int(raw, "max_tokens"),
    )


def _parse_session(raw: dict[str, Any]) -> SessionConfig:
    return SessionConfig(
        work_dir=str(raw.get("work_dir", ".")),
        mak_dir=str(raw.get("mak_dir", ".mak")),
        max_concurrent_agents=_as_int(raw, "max_concurrent_agents", 3),
        lock_timeout_s=_as_float(raw, "lock_timeout_s", 300.0),
        deadlock_check_interval_s=_as_float(
            raw, "deadlock_check_interval_s", 5.0),
        test_command=_opt_str(raw, "test_command"),
    )


_PLANNER_STRATEGIES = ("oneshot", "outline")


def _parse_planner(raw: dict[str, Any]) -> PlannerConfig:
    strategy = str(raw.get("strategy", "oneshot"))
    if strategy not in _PLANNER_STRATEGIES:
        raise ConfigError(
            f"planner 'strategy' must be one of {_PLANNER_STRATEGIES}, got {strategy!r}"
        )
    return PlannerConfig(
        model=str(raw.get("model", "")),
        max_retries=_as_int(raw, "max_retries", 3),
        validate=_as_bool(raw, "validate", True),
        strategy=strategy,
        self_critique=_as_bool(raw, "self_critique", False),
    )


def _parse_models(raw: dict[str, Any]) -> ModelsConfig:
    return ModelsConfig(auto_refresh=_as_bool(raw, "auto_refresh", True))


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


# Models that work with MAK but carry caveats the user must know about before
# a run burns tokens (or 400s). Checked wherever a model is chosen: the TUI's
# /models, /planner, and setup wizard, and `mak run`'s config/--models path.
_MODEL_CAVEATS: dict[str, str] = {
    "claude-fable": (
        "claude-fable-5 requires an org with 30-day data retention "
        "(zero-data-retention orgs get a 400 on every request), can decline "
        "requests with a 'refusal' stop reason — which MAK treats as a failed "
        "task — and is priced above Opus tier ($10/$50 per MTok)."
    ),
}


def model_caveat(model_id: str | None) -> str | None:
    """Return the usage caveat for ``model_id``, or None if it has none."""
    if not model_id:
        return None
    for prefix, caveat in _MODEL_CAVEATS.items():
        if model_id.startswith(prefix):
            return caveat
    return None


def user_config_dir() -> Path:
    """Return MAK's per-user config directory (respects ``XDG_CONFIG_HOME``).

    This is where an installed MAK looks for user-level state: a custom
    ``config.yaml`` and the ``.env`` file holding API keys.
    """
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "mak"


def packaged_config_path() -> Path:
    """Return the default ``config.yaml`` shipped inside the ``mak`` package."""
    return Path(__file__).resolve().parent / "config.yaml"


def discover_config_path() -> Path:
    """Return the config file to use when none is given explicitly.

    Discovery order:

    1. ``./mak.yaml`` — a per-project config in the current directory.
    2. ``<user config dir>/config.yaml`` — e.g. ``~/.config/mak/config.yaml``.
    3. The packaged default (``mak/config.yaml`` inside the installed package;
       in a source checkout this is the repo's ``mak/config.yaml``).
    """
    project_config = Path("mak.yaml")
    if project_config.is_file():
        return project_config
    user_config = user_config_dir() / "config.yaml"
    if user_config.is_file():
        return user_config
    return packaged_config_path()


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
        models=_parse_models(data.get("models", {})),
    )
