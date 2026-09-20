"""Configuration loading, discovery, and validation for MAK."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from mak.core.exceptions import ConfigError
from mak.endpoints.parse import parse_endpoints
from mak.endpoints.types import (
    EndpointConfig,
    validate_agent_id,
    validate_endpoint_id,
)
from mak.node_store.store import (
    DEFAULT_VERSION_RETENTION,
    MIN_VERSION_RETENTION,
    UNBOUNDED_VERSION_RETENTION,
)

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


_STRUCTURED_OUTPUT_MODES = ("json_object", "json_schema", "none")
_PLANNER_BACKENDS = ("anthropic", "openai", "gemini", "ollama")


def normalize_base_url(value: str, *, where: str) -> str:
    """Return a validated endpoint URL with any trailing slash stripped.

    Shared by ``mak.bootstrap.agents_from_specs``, this module's own parsing, and
    the TUI's ``/local`` wizard, so a URL typed on the command line, one written
    in YAML, and one entered interactively are validated by exactly one rule —
    and a user who is told "http:// or https:// required" once is told it
    everywhere. ``where`` names the setting so the message points at the line to
    fix.
    """
    text = value.strip()
    if not text:
        raise ConfigError(f"{where} must not be empty")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ConfigError(
            f"{where} must be an http:// or https:// URL, got {value!r} "
            "(e.g. http://localhost:11434)"
        )
    if not parts.netloc:
        raise ConfigError(
            f"{where} must include a host, got {value!r} "
            "(e.g. http://localhost:11434)"
        )
    return text.rstrip("/")


def _opt_url(raw: dict[str, Any], key: str) -> str | None:
    """Return an optional endpoint URL setting, or None when unset or empty."""
    value = raw.get(key)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return normalize_base_url(text, where=f"'{key}'")


def _as_choice(
    raw: dict[str, Any], key: str, default: str | None, allowed: tuple[str, ...]
) -> str | None:
    """Return an optional enumerated setting, rejecting a typo **at load time**.

    A misspelled mode must fail before a run starts rather than at dispatch,
    where it would surface as a provider 400 halfway through a wave.
    """
    value = raw.get(key, default)
    if value is None:
        return None
    text = str(value)
    if text not in allowed:
        raise ConfigError(
            f"'{key}' must be one of {allowed}, got {text!r}"
        )
    return text


def _require_choice(
    raw: dict[str, Any], key: str, default: str, allowed: tuple[str, ...]
) -> str:
    """Return an enumerated setting that always has a value.

    Distinct from :func:`_as_choice`, whose ``None`` means "unset, let the layer
    below decide". These settings are *policies* — what to do about a file edited
    outside MAK, whether a skipped suite may push — and a policy with no value is
    not a thing MAK can act on, so the default is a real choice rather than a
    deferral.
    """
    value = raw.get(key, default)
    text = str(value)
    if text not in allowed:
        raise ConfigError(f"'{key}' must be one of {allowed}, got {text!r}")
    return text


def _opt_non_negative_int(raw: dict[str, Any], key: str) -> int | None:
    """Return an optional int setting that may be zero, or None when unset.

    Distinct from :func:`_opt_positive_int` because ``0`` is meaningful for
    ``repair_attempts``: it is how a user switches the repair turn off.
    """
    value = raw.get(key)
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' must be an integer, got {value!r}") from exc
    if number < 0:
        raise ConfigError(f"'{key}' must not be negative, got {number}")
    return number


def _opt_float(raw: dict[str, Any], key: str) -> float | None:
    """Return an optional float setting, or None when unset."""
    value = raw.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{key}' must be a number, got {value!r}") from exc


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

    The remaining six parameterize a **local** transport (``local_api`` over any
    OpenAI-compatible server, or ``ollama_api`` over Ollama's native API). Every
    one is ``None`` when unset so the *adapter* stays the single place that owns
    each default, the same rule ``max_tokens`` states above.

    ``base_url`` is the endpoint. It is required for ``local_api``, optional for
    ``openai_api`` (a gateway or proxy), and defaulted for ``ollama_api``.
    Setting it also arms a security rule the adapter enforces: a real
    ``OPENAI_API_KEY`` is **never** forwarded to a ``base_url`` endpoint — MAK
    sends the configured ``api_key_env``'s value if one was named, else the
    literal placeholder ``"local"``, and never lets the SDK read the environment
    itself.

    ``structured_output`` picks how the reply's shape is constrained:
    ``json_object`` (the OpenAI-compatible default, today's behaviour),
    ``json_schema`` (strict schema / constrained decoding — the ``ollama_api``
    default, where it is native and free), or ``none``. A call rejected for
    naming an unsupported response format is retried once one rung down.

    ``repair_attempts`` bounds the adapter's follow-up turn after a malformed
    reply (adapter default 1; ``0`` disables it). A decode failure would
    otherwise cost a full re-dispatch of the whole bundle — tens of KB — which
    for a small local model is the common case, not the rare one.

    ``num_ctx`` / ``keep_alive`` / ``temperature`` apply to ``ollama_api`` only.
    Ollama's runtime context defaults to a few thousand tokens regardless of what
    the model supports and **silently truncates** an over-long prompt, so the
    adapter sizes the window itself; ``num_ctx`` overrides that sizing.
    ``keep_alive`` (e.g. ``"30m"``) keeps the model resident between tasks, which
    is otherwise a multi-second reload per task. ``temperature`` unset leaves the
    server's own default, which is tuned for chat rather than for code.

    ``id`` and ``endpoint`` are Wave 22's separation of concerns. ``id`` is the
    **routing key**: the registry, scheduler, pool caps, planner choice, logs and
    git metadata all key on it, which is what lets two agents share an adapter
    class (two OpenAI-compatible endpoints, say) without one overwriting the
    other. Unset, it is derived from ``type`` so every existing config keeps
    today's behaviour exactly.

    ``endpoint`` names an entry in the top-level ``endpoints:`` section. When it
    is set, ``type`` is *derived* from that endpoint's transport and must not be
    written by hand — an entry naming both an endpoint and a contradictory
    ``type``/``base_url``/``api_key_env`` is rejected rather than silently
    resolved one way.
    """

    type: str
    max_instances: int = 2
    timeout: int = 300
    model: str | None = None
    api_key_env: str | None = None
    cmd: str | None = None
    max_tokens: int | None = None
    base_url: str | None = None
    structured_output: str | None = None
    repair_attempts: int | None = None
    num_ctx: int | None = None
    keep_alive: str | None = None
    temperature: float | None = None
    id: str | None = None
    endpoint: str | None = None

    def routing_id(self) -> str:
        """Return this agent's routing key: the explicit id, else the type.

        The fallback is what keeps every pre-Wave-22 config behaving identically
        — a roster of ``anthropic_api``/``openai_api``/``gemini_api`` derives
        exactly the ids the registry used to key on.
        """
        return self.id or self.type


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Session-level configuration.

    ``test_command`` is the shell command MAK runs in the work dir during teardown
    to gate an ``auto_push`` (and to report a pass/fail after a run). ``None``
    (the default) skips the test step entirely — nothing is run and teardown
    reports success. Set e.g. ``"pytest -q"`` to make the gate real.

    ``dependency_context_bytes`` bounds the source a bundle carries from the tasks
    it ``depends_on``. A dependent task must see what its dependencies built — that
    is the whole point of the edge — but whole files are the most expensive thing a
    bundle can hold. Past the budget an entry degrades to a public API digest
    instead of being dropped, so the task is never blind: ``0`` disables the layer
    entirely and ``-1`` makes it unbounded.

    ``cross_file_context_bytes`` bounds the *caller* layer the same way, and needs
    to: one observed bundle spent 151 KB (67,847 input tokens) on a single task
    because that layer had no ceiling at all. Past this budget an entry is
    **dropped** rather than digested — a caller's value is its call site, and a
    signature digest of a caller says nothing. ``0`` disables the layer, ``-1``
    makes it unbounded.

    ``max_total_tokens`` is the run's spend ceiling: input plus output, agents
    plus planner, counted from what each provider reported on its own response.
    Nothing else bounds a run's cost — ``max_attempts`` × ``max_iterations`` ×
    cascade waves × per-agent output multiply out to no ceiling at all — so this
    is the only way to cap it up front. ``None`` (the default) is unbounded. On a
    breach the run stops dispatching, finishes what is already in flight, and
    reports failure naming the budget; it never interrupts a commit.

    ``on_external_edit`` decides what startup reconciliation does with a file
    that changed since MAK last wrote it — a human's edit between two sessions.
    ``"adopt"`` (the default) treats the working tree as the newer truth and
    synchronizes the store to it, including symbols that were deleted or renamed.
    ``"conflict"`` refuses to continue, raising before planning so no agent is
    handed content the tree no longer holds.

    ``test_policy`` decides whether a push may happen when no test suite ran.
    ``"require_pass"`` (the default) means only a genuinely passing suite opens
    the push gate — a project with no ``test_command`` configured never pushes,
    because "no tests ran" is not "the tests passed". ``"allow_skip"`` is the
    deliberate opt-out for a project that has no suite and wants ``auto_push``
    anyway.
    """

    work_dir: str = "."
    mak_dir: str = ".mak"
    max_concurrent_agents: int = 3
    lock_timeout_s: float = 300.0
    deadlock_check_interval_s: float = 5.0
    test_command: str | None = None
    dependency_context_bytes: int = 24000
    cross_file_context_bytes: int = 32000
    max_total_tokens: int | None = None
    on_external_edit: str = "adopt"
    test_policy: str = "require_pass"


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

    ``backend`` / ``base_url`` / ``api_key_env`` are how a planner reaches a
    runtime the model id cannot name. Backend resolution is explicit, then
    transport, then name: ``backend`` when set (``anthropic`` / ``openai`` /
    ``gemini`` / ``ollama``), else the OpenAI-compatible client when ``base_url``
    is set, else the model-id prefix routing. A local model id
    (``qwen2.5-coder:14b``, ``llama3.1``) matches no prefix and would otherwise
    fail the run before a single call. ``api_key_env`` names the variable holding
    a token for a protected gateway (``vllm --api-key``); a local runtime needs
    none, and leaving it unset is what lets the placeholder-key rule apply.

    ``endpoint`` (Wave 22) is the authoritative planner route when set: it names
    an entry in the top-level ``endpoints:`` section and supersedes both the
    model-prefix inference and the legacy ``backend``/``base_url`` pair. Naming
    an endpoint *and* a contradictory ``backend``/``base_url``/``api_key_env`` is
    rejected at load rather than silently resolved — a planner pointed at the
    wrong host is a privacy failure, not a preference.
    """

    model: str = ""
    max_retries: int = 3
    validate: bool = True
    strategy: str = "oneshot"
    self_critique: bool = False
    backend: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    endpoint: str | None = None


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
    """Git integration configuration.

    ``require_clean_tree`` is an opt-in precondition: when on, a session refuses
    to start if the working tree has uncommitted changes, so ``git diff`` after a
    run means exactly "what MAK did". It is off by default because that is a
    product policy a project chooses, not one MAK is entitled to impose — MAK's
    audit commits are path-scoped either way and never absorb unrelated work.
    """

    auto_commit: bool = True
    auto_push: bool = False
    commit_prefix: str = "[MAK]"
    require_clean_tree: bool = False


@dataclass(frozen=True, slots=True)
class NodeStoreConfig:
    """Node store file-matching and retention configuration.

    ``version_retention`` bounds how many on-disk versions of a node survive.
    Every commit writes a ``v{n}.py`` and nothing used to remove one, so
    ``.mak/node_store/`` grew monotonically for the life of a project. The
    committed version plus ``N-1`` prior are kept; the floor is 2 because
    ``revert_node`` needs a prior version to roll back to, and ``-1`` restores
    the old unbounded behaviour.
    """

    include_patterns: tuple[str, ...] = tuple(_DEFAULT_INCLUDE)
    exclude_patterns: tuple[str, ...] = tuple(_DEFAULT_EXCLUDE)
    version_retention: int = DEFAULT_VERSION_RETENTION


STALE_READ_POLICIES = (
    "accept_if_api_stable",
    "revalidate",
    "redispatch",
    "reject",
)
TYPE_CHECKERS = ("off", "pyright", "mypy")
_ON_OFF = ("off", "on")


@dataclass(frozen=True, slots=True)
class SemanticConfig:
    """Semantic-conflict prevention, detection and the optional gates (Wave 20).

    Node write locks rule out *textual* conflicts. These settings govern the
    *semantic* ones — two edits on disjoint nodes that are each right and wrong
    together.

    ``stale_read`` is what a commit does when a node its bundle carried was
    committed by someone else while the task was in flight:

    - ``accept_if_api_stable`` — accept a body-only change, re-dispatch on any
      interface change;
    - ``revalidate`` (default) — accept a body-only change, and an interface
      change the task's code does not use, or that the static checks re-verify
      against the new code; re-dispatch (with the diff) on anything uncertain;
    - ``redispatch`` — re-dispatch on every stale read (strict snapshot
      isolation);
    - ``reject`` — reject non-body-only stale reads like any other conflict.

    ``api_locks`` splits each node's lock into interface (``node#api``) and body
    (``node``): callers read-lock the interface, a declared body-only edit takes
    only the body, and an interface change is enforced at commit.
    ``intention_locks`` makes a fragment write hold INTENT_WRITE on its file (and
    a method on its class), so a whole-file or whole-class writer cannot run
    beside it. ``registry_keys`` treats *keyed* registrar functions
    (``register("<key>", ...)`` lists) as commutative: appenders run in parallel,
    the kernel merges their lines, and a repeated key is a collision. All three
    are ablation flags for the scaling study and default on.

    ``contract_dispatch`` (opt-in) dispatches a dependent against its provider's
    declared contract instead of waiting for the implementation to commit.

    The heavy gates are all off by default and never fail a wave: each finding
    becomes a fix-up task. ``type_check`` diffs pyright/mypy diagnostics against
    a baseline taken at ``initialize``; ``impact_tests`` runs the tests that
    import what the wave touched and attributes new failures to a task or task
    pair; ``import_smoke`` imports every touched module in a fresh interpreter;
    ``adjudicator`` (``"<backend>:<model>"``) asks a cheap model about stale
    reads the static checks cannot settle, at most ``adjudicator_max_calls`` per
    wave. ``gate_timeout_s`` bounds every gate subprocess and
    ``impact_max_overlays`` bounds how many subset states pairwise attribution
    may build.
    """

    stale_read: str = "revalidate"
    api_locks: bool = True
    intention_locks: bool = True
    registry_keys: bool = True
    contract_dispatch: bool = False
    type_check: str = "off"
    impact_tests: bool = False
    import_smoke: bool = False
    adjudicator: str | None = None
    adjudicator_max_calls: int = 5
    gate_timeout_s: float = 300.0
    impact_max_overlays: int = 12


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
    semantic: SemanticConfig = field(default_factory=SemanticConfig)
    # Declared endpoints, portable and secret-free: a project may commit these
    # so a teammate inherits the URLs and credential *variable names* without
    # inheriting anyone's key. User-created endpoints live in the per-user store
    # (``mak.endpoints.store``) and are merged on top at composition time.
    endpoints: tuple[EndpointConfig, ...] = ()


# Settings an endpoint already decides. Writing one *and* naming an endpoint is
# rejected rather than resolved: whichever way MAK broke the tie, half of the
# users who wrote it would get the other host — and one of those outcomes sends
# a credential somewhere the config did not name.
_ENDPOINT_OWNED_AGENT_FIELDS: tuple[str, ...] = ("type", "base_url", "api_key_env")


def _parse_agent(raw: dict[str, Any]) -> AgentConfig:
    endpoint = _opt_str(raw, "endpoint")
    if endpoint is not None:
        return _parse_endpoint_agent(raw, endpoint)
    if "type" not in raw:
        raise ConfigError(
            "each agent entry must have a 'type' field (or an 'endpoint' "
            "naming an entry in the 'endpoints:' section)"
        )
    return AgentConfig(
        type=str(raw["type"]),
        id=_opt_agent_id(raw),
        max_instances=_as_int(raw, "max_instances", 2),
        timeout=_as_int(raw, "timeout", 300),
        model=_opt_str(raw, "model"),
        api_key_env=_opt_str(raw, "api_key_env"),
        cmd=_opt_str(raw, "cmd"),
        max_tokens=_opt_positive_int(raw, "max_tokens"),
        base_url=_opt_url(raw, "base_url"),
        structured_output=_as_choice(
            raw, "structured_output", None, _STRUCTURED_OUTPUT_MODES
        ),
        repair_attempts=_opt_non_negative_int(raw, "repair_attempts"),
        num_ctx=_opt_positive_int(raw, "num_ctx"),
        keep_alive=_opt_str(raw, "keep_alive"),
        temperature=_opt_float(raw, "temperature"),
    )


def _opt_agent_id(raw: dict[str, Any]) -> str | None:
    """Return the agent's explicit routing id, validated, or None when unset."""
    value = raw.get("id")
    if value is None:
        return None
    return validate_agent_id(str(value))


def _parse_endpoint_agent(raw: dict[str, Any], endpoint: str) -> AgentConfig:
    """Parse an agent entry that names an endpoint rather than a type.

    The adapter ``type`` is *derived* from the endpoint's transport, so the
    canonical form carries no redundant transport field. Generation limits
    (``max_tokens``, ``timeout``, ``num_ctx`` …) stay on the agent, because they
    belong to this model's use of the endpoint rather than to the endpoint.
    """
    endpoint_id = validate_endpoint_id(endpoint)
    contradictions = [f for f in _ENDPOINT_OWNED_AGENT_FIELDS if f in raw]
    if contradictions:
        raise ConfigError(
            f"agent entry for endpoint '{endpoint_id}' also sets "
            f"{', '.join(repr(f) for f in contradictions)}, which the endpoint "
            "already decides. Remove them from the agent, or drop 'endpoint' "
            "and configure the agent the legacy way — MAK will not guess which "
            "host you meant."
        )
    return AgentConfig(
        # Filled in by resolution from the endpoint's transport; the placeholder
        # is never used to construct anything, because an endpoint-backed agent
        # always goes through ``mak.endpoints.resolution``.
        type="",
        id=_opt_agent_id(raw),
        endpoint=endpoint_id,
        max_instances=_as_int(raw, "max_instances", 2),
        timeout=_as_int(raw, "timeout", 300),
        model=_opt_str(raw, "model"),
        max_tokens=_opt_positive_int(raw, "max_tokens"),
        structured_output=_as_choice(
            raw, "structured_output", None, _STRUCTURED_OUTPUT_MODES
        ),
        repair_attempts=_opt_non_negative_int(raw, "repair_attempts"),
        num_ctx=_opt_positive_int(raw, "num_ctx"),
        keep_alive=_opt_str(raw, "keep_alive"),
        temperature=_opt_float(raw, "temperature"),
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
        dependency_context_bytes=_as_int(raw, "dependency_context_bytes", 24000),
        cross_file_context_bytes=_as_int(raw, "cross_file_context_bytes", 32000),
        max_total_tokens=_opt_positive_int(raw, "max_total_tokens"),
        on_external_edit=_require_choice(
            raw, "on_external_edit", "adopt", _EXTERNAL_EDIT_POLICIES
        ),
        test_policy=_require_choice(
            raw, "test_policy", "require_pass", _TEST_POLICIES
        ),
    )


_EXTERNAL_EDIT_POLICIES = ("adopt", "conflict")
_TEST_POLICIES = ("require_pass", "allow_skip")
_PLANNER_STRATEGIES = ("oneshot", "outline")


# Planner settings an endpoint already decides. Same reasoning as
# ``_ENDPOINT_OWNED_AGENT_FIELDS``, with more at stake: the planner is what sees
# the whole repository inventory, so an ambiguous route is a privacy question.
_ENDPOINT_OWNED_PLANNER_FIELDS: tuple[str, ...] = (
    "backend",
    "base_url",
    "api_key_env",
)


def _parse_planner(raw: dict[str, Any]) -> PlannerConfig:
    strategy = str(raw.get("strategy", "oneshot"))
    if strategy not in _PLANNER_STRATEGIES:
        raise ConfigError(
            f"planner 'strategy' must be one of {_PLANNER_STRATEGIES}, got {strategy!r}"
        )
    endpoint = _opt_str(raw, "endpoint")
    if endpoint is not None:
        endpoint = validate_endpoint_id(endpoint)
        contradictions = [f for f in _ENDPOINT_OWNED_PLANNER_FIELDS if f in raw]
        if contradictions:
            raise ConfigError(
                f"planner names endpoint '{endpoint}' and also sets "
                f"{', '.join(repr(f) for f in contradictions)}, which the "
                "endpoint already decides. The planner sees your whole "
                "repository inventory — MAK will not guess which host you meant."
            )
    return PlannerConfig(
        endpoint=endpoint,
        model=str(raw.get("model", "")),
        max_retries=_as_int(raw, "max_retries", 3),
        validate=_as_bool(raw, "validate", True),
        strategy=strategy,
        self_critique=_as_bool(raw, "self_critique", False),
        backend=_as_choice(raw, "backend", None, _PLANNER_BACKENDS),
        base_url=_opt_url(raw, "base_url"),
        api_key_env=_opt_str(raw, "api_key_env"),
    )


def _on_off(raw: dict[str, Any], key: str) -> bool:
    """Read an ``off``/``on`` gate switch; YAML booleans are accepted too."""
    value = raw.get(key, "off")
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text not in _ON_OFF:
        raise ConfigError(f"'{key}' must be one of {_ON_OFF}, got {value!r}")
    return text == "on"


def _parse_adjudicator(raw: dict[str, Any]) -> str | None:
    """Return ``"<backend>:<model>"`` for the adjudicator, or None when off."""
    value = raw.get("adjudicator")
    if value is None or value is False:
        return None
    text = str(value).strip()
    if text.lower() in ("", "off"):
        return None
    backend, sep, model = text.partition(":")
    if not sep or backend not in _PLANNER_BACKENDS or not model.strip():
        raise ConfigError(
            "'adjudicator' must be 'off' or '<backend>:<model>' with a backend "
            f"in {_PLANNER_BACKENDS}, got {value!r}"
        )
    return f"{backend}:{model.strip()}"


def _parse_semantic(raw: dict[str, Any]) -> SemanticConfig:
    """Parse the ``semantic:`` section, rejecting a typo at load time."""
    type_check = raw.get("type_check", "off")
    type_check = "off" if type_check is False else str(type_check).strip().lower()
    if type_check not in TYPE_CHECKERS:
        raise ConfigError(
            f"'type_check' must be one of {TYPE_CHECKERS}, got {type_check!r}"
        )
    max_calls = _as_int(raw, "adjudicator_max_calls", 5)
    overlays = _as_int(raw, "impact_max_overlays", 12)
    timeout = _as_float(raw, "gate_timeout_s", 300.0)
    if max_calls < 0 or overlays < 0 or timeout <= 0:
        raise ConfigError(
            "'adjudicator_max_calls' and 'impact_max_overlays' must not be "
            "negative, and 'gate_timeout_s' must be positive"
        )
    return SemanticConfig(
        stale_read=_require_choice(
            raw, "stale_read", "revalidate", STALE_READ_POLICIES
        ),
        api_locks=_as_bool(raw, "api_locks", True),
        intention_locks=_as_bool(raw, "intention_locks", True),
        registry_keys=_as_bool(raw, "registry_keys", True),
        contract_dispatch=_as_bool(raw, "contract_dispatch", False),
        type_check=type_check,
        impact_tests=_on_off(raw, "impact_tests"),
        import_smoke=_on_off(raw, "import_smoke"),
        adjudicator=_parse_adjudicator(raw),
        adjudicator_max_calls=max_calls,
        gate_timeout_s=timeout,
        impact_max_overlays=overlays,
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
    retention = _as_int(raw, "version_retention", DEFAULT_VERSION_RETENTION)
    if retention != UNBOUNDED_VERSION_RETENTION and retention < MIN_VERSION_RETENTION:
        raise ConfigError(
            f"'version_retention' must be at least {MIN_VERSION_RETENTION} "
            f"(or {UNBOUNDED_VERSION_RETENTION} for unbounded), got {retention}"
        )
    return NodeStoreConfig(
        include_patterns=tuple(str(p) for p in include),
        exclude_patterns=tuple(str(p) for p in exclude),
        version_retention=retention,
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


def anchor_mak_dir(config: MakConfig) -> MakConfig:
    """Resolve ``session.mak_dir`` to an absolute path inside ``session.work_dir``.

    A relative ``mak_dir`` (the default ``.mak``) used to be interpreted against
    the *process* working directory while ``work_dir`` pointed somewhere else
    entirely. Two projects driven from one shell therefore shared a single node
    store — and because node ids are work-dir-relative, ``toolkit/registry.py``
    in one project and the other were literally the same id. Re-ingestion is
    skipped when a whole-file node already exists, so the second project
    inherited the first's content and reconstruction wrote it to disk.

    Anchoring removes the ambiguity: a project's state lives with the project. An
    absolute ``mak_dir`` is honored as an explicit override and left alone.
    """
    mak_dir = Path(config.session.mak_dir)
    if mak_dir.is_absolute():
        return config
    anchored = (Path(config.session.work_dir) / mak_dir).resolve()
    return replace(
        config, session=replace(config.session, mak_dir=str(anchored))
    )


def stale_mak_dir(config: MakConfig) -> Path | None:
    """Return a CWD-relative ``.mak`` that this run will *not* use, if one exists.

    Call **before** :func:`anchor_mak_dir`. A store left by an older MAK sits
    where the old interpretation put it — beside the shell, not beside the
    project — and after anchoring it is simply ignored, which is silent and
    confusing when a run suddenly re-ingests everything.

    Reported, never adopted: deciding that an orphaned store belongs to *this*
    project means guessing, and guessing wrong reintroduces exactly the
    cross-project contamination anchoring fixes.
    """
    mak_dir = Path(config.session.mak_dir)
    if mak_dir.is_absolute():
        return None
    previous = (Path.cwd() / mak_dir).resolve()
    anchored = (Path(config.session.work_dir) / mak_dir).resolve()
    if previous == anchored or not previous.is_dir():
        return None
    return previous


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


def examples_dir() -> Path:
    """Return the directory of packaged example configs (``mak/examples/``)."""
    return Path(__file__).resolve().parent / "examples"


def list_examples() -> list[str]:
    """Return the names of the packaged example configs, alphabetically.

    Names carry no ``.yaml`` suffix, so ``mak examples local-ollama > mak.yaml``
    reads as one thought.
    """
    directory = examples_dir()
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.yaml"))


def example_path(name: str) -> Path:
    """Return the path of one packaged example, or raise naming what exists.

    Rejects any name that is not a plain file in the examples directory: the
    argument reaches here from the command line, and joining an arbitrary string
    to a package path is how a "print my config" command becomes a file read.
    """
    candidate = (examples_dir() / f"{name}.yaml").resolve()
    if (
        candidate.parent != examples_dir()
        or not candidate.is_file()
    ):
        available = ", ".join(list_examples()) or "none"
        raise ConfigError(
            f"no packaged example named {name!r}; available: {available}"
        )
    return candidate


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

    config = MakConfig(
        session=_parse_session(data.get("session", {})),
        planner=_parse_planner(data.get("planner", {})),
        agents=agents,
        git=_parse_git(data.get("git", {})),
        node_store=_parse_node_store(data.get("node_store", {})),
        models=_parse_models(data.get("models", {})),
        semantic=_parse_semantic(_section(data, "semantic")),
        endpoints=parse_endpoints(data.get("endpoints")),
    )
    check_endpoint_references(config)
    return config


def check_endpoint_references(config: MakConfig) -> None:
    """Raise ``ConfigError`` if an agent or the planner names a missing endpoint.

    Run at load rather than at composition so a typo in an endpoint id fails
    where the user can see the file, not as an ``UnknownAgentTypeError`` once a
    run is already holding locks.

    A reference may legitimately resolve against the *user* endpoint store,
    which this module does not read. So the check runs only when the file
    declares endpoints of its own: that is the single-file case, where a missing
    name is unambiguously a typo. The composition root re-checks every
    reference against the merged set once the user store has been loaded.
    """
    declared = {e.id for e in config.endpoints}
    if not declared:
        return
    for agent in config.agents:
        if agent.endpoint and agent.endpoint not in declared:
            raise ConfigError(
                f"agent '{agent.routing_id()}' names endpoint "
                f"'{agent.endpoint}', which this config does not declare; "
                f"declared endpoints: {', '.join(sorted(declared))}"
            )
    planner_endpoint = config.planner.endpoint
    if planner_endpoint and planner_endpoint not in declared:
        raise ConfigError(
            f"planner names endpoint '{planner_endpoint}', which this config "
            f"does not declare; declared endpoints: {', '.join(sorted(declared))}"
        )


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a config section as a mapping, refusing a non-mapping value."""
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be a mapping")
    return value
