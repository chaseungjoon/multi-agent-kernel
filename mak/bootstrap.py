"""Composition root: build runtime collaborators from a ``MakConfig``.

The ``Session`` takes every collaborator injected (so it stays testable). Something
has to assemble those collaborators from configuration for a real run — that is
this module. A CLI entry point is a thin shell over ``build_registry`` and
``default_agent_type``; keeping the wiring here means it is unit-testable without
parsing argv.

Adapters are registered as **config-bound factories**: each factory closes over
the agent's configured ``model`` and the API key resolved from its ``api_key_env``
at build time, which the zero-arg ``register(cls)`` path cannot carry. SDK clients
are still constructed lazily inside the adapter, so building a registry performs
**no network call** and needs no key present.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.agent_runner.adapters.claude_code_adapter import ClaudeCodeAdapter
from mak.agent_runner.adapters.codex_adapter import CodexAdapter
from mak.agent_runner.adapters.copilot_adapter import CopilotAdapter
from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter
from mak.agent_runner.adapters.ollama_api_adapter import OllamaApiAdapter
from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.agent_runner.registry import AdapterRegistry
from mak.agent_runner.sandbox import SandboxConfig
from mak.config import AgentConfig, MakConfig, normalize_base_url
from mak.core.exceptions import AgentError, ConfigError
from mak.local.discovery import LOCAL_BASE_URL_ENV
from mak.local.ollama_client import DEFAULT_BASE_URL as OLLAMA_DEFAULT_BASE_URL

# Agent types with a built, first-party API adapter — each takes ``model`` +
# ``api_key`` kwargs. ``Callable[..., AgentAdapter]`` keeps the generic factory
# below honest without naming each constructor's full (and differing) signature.
_API_ADAPTER_CLASSES: dict[str, Callable[..., AgentAdapter]] = {
    "anthropic_api": AnthropicApiAdapter,
    "openai_api": OpenAiApiAdapter,
    "gemini_api": GeminiApiAdapter,
    # Same class as ``openai_api``, registered under its own type on purpose
    # (D1). The registry is keyed by agent *type*, so with one shared type a run
    # could have cloud OpenAI **or** a local model and never both — and nothing
    # downstream (the health preflight, the planner's agent-type list, warnings,
    # logs, the TUI) could tell the two apart. The instance is told which name
    # it was built under and reports that.
    "local_api": OpenAiApiAdapter,
    "ollama_api": OllamaApiAdapter,
}

# Types reached over the OpenAI Chat Completions wire format. ``openai_api`` is
# in the set because a ``base_url`` is legitimate there too (a gateway or proxy).
_OPENAI_COMPATIBLE_TYPES: frozenset[str] = frozenset({"openai_api", "local_api"})

# Every type that accepts a ``base_url`` and the structured-output/repair knobs.
# The Anthropic and Gemini constructors take none of them, so an unconditional
# kwarg would be a ``TypeError`` at dispatch rather than a config error at start.
_LOCAL_TYPES: frozenset[str] = _OPENAI_COMPATIBLE_TYPES | {"ollama_api"}

# Options only the native Ollama adapter accepts.
_OLLAMA_ONLY_OPTIONS: tuple[str, ...] = ("num_ctx", "keep_alive", "temperature")

# Options every local transport accepts.
_LOCAL_OPTIONS: tuple[str, ...] = ("base_url", "structured_output", "repair_attempts")

# The agent types that run against a model on this machine (or on a host the
# user named), rather than a hosted provider. Public because callers outside the
# composition root need to ask "is this run local?" — ``mak.__main__``'s
# planner-mismatch warning and the TUI's mode both do.
LOCAL_AGENT_TYPES: frozenset[str] = frozenset({"local_api", "ollama_api"})

# Secondary CLI adapter types — each takes a ``cmd`` override and an optional
# ``sandbox``. They are fallbacks; the API adapters above are primary.
_CLI_ADAPTER_CLASSES: dict[str, Callable[..., AgentAdapter]] = {
    "claude_code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "copilot": CopilotAdapter,
}

# Every agent type the kernel knows how to build. Config validation rejects
# anything outside this set (catches typos before a run starts).
KNOWN_AGENT_TYPES: frozenset[str] = frozenset(_API_ADAPTER_CLASSES) | frozenset(
    _CLI_ADAPTER_CLASSES
)

# Friendly provider name (used on the command line) -> (adapter type, conventional
# API-key env var). MAK's hosted-model support is exactly these three providers.
_PROVIDER_TO_API: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic_api", "ANTHROPIC_API_KEY"),
    "openai": ("openai_api", "OPENAI_API_KEY"),
    "gemini": ("gemini_api", "GEMINI_API_KEY"),
    "google": ("gemini_api", "GEMINI_API_KEY"),  # alias for gemini
}

# Friendly provider names for the local transports. They carry no API-key env
# var, because a local runtime has none by construction — which is also why they
# are deliberately absent from ``DEFAULT_KEY_ENV`` and ``_PROVIDER_TO_API``,
# both of which map *keyed hosted* providers.
_PROVIDER_TO_LOCAL: dict[str, str] = {
    "local": "local_api",
    "ollama": "ollama_api",
}

# The providers we advertise (aliases like "google" are accepted but not listed).
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "anthropic",
    "openai",
    "gemini",
    "local",
    "ollama",
)

# Providers whose spec may carry an ``@base_url``: the two local ones, plus
# OpenAI, where it points at a gateway or proxy. Anthropic and Gemini have no
# such notion, so accepting one there would silently ignore it.
_BASE_URL_PROVIDERS: frozenset[str] = frozenset({"openai", "local", "ollama"})

_SPEC_SYNTAX = (
    "provider[:model][@base_url] — e.g. anthropic:claude-opus-5, "
    "ollama:qwen2.5-coder:14b, local:my-model@http://localhost:8000/v1"
)

# Adapter type -> conventional API-key env var, for resolving the planner's key
# when the roster does not happen to include that provider.
DEFAULT_KEY_ENV: dict[str, str] = {
    agent_type: key_env for agent_type, key_env in _PROVIDER_TO_API.values()
}


def _split_spec(spec: str) -> tuple[str, str, str]:
    """Split ``provider[:model][@base_url]`` into its three parts.

    Two deliberate choices about *which* separator wins:

    - the **first** ``@`` ends the body, not the last. A URL may carry a
      userinfo segment (``http://user:pass@host/v1``) and ``rpartition`` would
      cut inside it; a model id never contains an ``@``.
    - the **first** ``:`` ends the provider. A model id *does* contain colons —
      Ollama tags are ``qwen2.5-coder:14b`` — so partitioning on the first one
      keeps the tag intact.
    """
    body, at, url = spec.partition("@")
    provider, colon, model = body.partition(":")
    return (
        provider.strip().lower(),
        model.strip() if colon else "",
        url.strip() if at else "",
    )


def _local_agent(provider: str, model: str, url: str, spec: str) -> AgentConfig:
    """Build the roster entry for a ``local:``/``ollama:`` spec.

    ``ollama`` gets a default endpoint because the provider name *is* the
    runtime; ``local`` gets none, because guessing Ollama's port for someone
    running vLLM is worse than asking.
    """
    if not model:
        raise ConfigError(
            f"{spec!r} names no model; a local runtime has no default model — "
            f"write {_SPEC_SYNTAX}"
        )
    base_url = url or os.environ.get(LOCAL_BASE_URL_ENV, "").strip()
    if not base_url and provider == "ollama":
        base_url = OLLAMA_DEFAULT_BASE_URL
    if not base_url:
        raise ConfigError(
            f"{spec!r} names no endpoint; give one as '@<url>' or export "
            f"{LOCAL_BASE_URL_ENV} — write {_SPEC_SYNTAX}"
        )
    return AgentConfig(
        type=_PROVIDER_TO_LOCAL[provider],
        model=model,
        base_url=normalize_base_url(base_url, where=f"--models entry {spec!r}"),
    )


def agents_from_specs(specs: list[str]) -> tuple[AgentConfig, ...]:
    """Build an agent roster from ``provider[:model][@base_url]`` specs.

    Each spec names a provider and, optionally, an explicit model after a colon
    (``anthropic:claude-opus-4-8``); with no model a hosted provider falls back
    to the adapter's built-in default. For a hosted provider the conventional
    API-key env var is attached, so keys are read from the environment and never
    passed on the command line.

    ``@<base_url>`` points a spec at a specific endpoint. It is accepted for
    ``openai`` (a gateway or proxy), ``local`` (any OpenAI-compatible server —
    vLLM, LM Studio, llama.cpp), and ``ollama`` (the native runtime), and
    rejected for ``anthropic``/``gemini``, which have no such notion and would
    otherwise ignore it silently.

    The adapter registry is keyed by agent *type*, so MAK runs **one model per
    provider** in a single session — repeating a provider is rejected with a clear
    message (use ``--max-agents`` to set how many workers run concurrently).
    """
    if not specs:
        raise ConfigError(f"--models needs at least one entry: {_SPEC_SYNTAX}")
    agents: list[AgentConfig] = []
    seen: dict[str, str] = {}
    for spec in specs:
        provider, model, url = _split_spec(spec)
        if provider not in _PROVIDER_TO_API and provider not in _PROVIDER_TO_LOCAL:
            raise ConfigError(
                f"unknown provider {provider!r}; MAK supports "
                f"{', '.join(SUPPORTED_PROVIDERS)} — write {_SPEC_SYNTAX}"
            )
        if url and provider not in _BASE_URL_PROVIDERS:
            raise ConfigError(
                f"provider {provider!r} does not take an '@<base_url>'; only "
                f"{', '.join(sorted(_BASE_URL_PROVIDERS))} do"
            )
        if provider in _PROVIDER_TO_LOCAL:
            agent = _local_agent(provider, model, url, spec)
        else:
            agent_type, key_env = _PROVIDER_TO_API[provider]
            agent = AgentConfig(
                type=agent_type,
                model=model or None,
                api_key_env=key_env,
                base_url=(
                    normalize_base_url(url, where=f"--models entry {spec!r}")
                    if url
                    else None
                ),
            )
        if agent.type in seen:
            raise ConfigError(
                f"provider {provider!r} given more than once; MAK runs one model per "
                f"provider — use --max-agents N to set how many agents run at once"
            )
        seen[agent.type] = provider
        agents.append(agent)
    return tuple(agents)


def _resolve_api_key(agent: AgentConfig) -> str | None:
    """Read the API key from the configured env var, if any. Never persisted."""
    if agent.api_key_env is None:
        return None
    return os.environ.get(agent.api_key_env)


def _api_factory(agent: AgentConfig) -> Callable[[], AgentAdapter]:
    """Build a zero-arg factory for a configured API adapter (lazy SDK client)."""
    cls = _API_ADAPTER_CLASSES[agent.type]

    def make() -> AgentAdapter:
        key = _resolve_api_key(agent)
        # Each unset option is *omitted* rather than passed as None, so the
        # adapter's own default (including "resolve the budget from the model
        # catalog") stays the single place that decides it.
        options: dict[str, Any] = {"api_key": key}
        if agent.model is not None:
            options["model"] = agent.model
        if agent.max_tokens is not None:
            options["max_tokens"] = agent.max_tokens
        # ``timeout`` was previously consumed only by the *subprocess* read loop,
        # so an API agent had no bound at all: a wedged provider call never
        # returned and the session hung shutting its pool down. The configured
        # per-agent value now reaches the SDK client that actually makes the call.
        options["timeout"] = float(agent.timeout)
        # Each new option reaches **only** the types whose constructor accepts
        # it: the Anthropic and Gemini adapters take none of them, so an
        # unconditional kwarg would be a TypeError at dispatch time.
        if agent.type in _LOCAL_TYPES:
            # So a ``local_api`` instance reports its own name rather than the
            # class default it shares with cloud OpenAI (D1).
            options["agent_type"] = agent.type
            for name in _LOCAL_OPTIONS:
                value = getattr(agent, name)
                if value is not None:
                    options[name] = value
        if agent.type == "ollama_api":
            for name in _OLLAMA_ONLY_OPTIONS:
                value = getattr(agent, name)
                if value is not None:
                    options[name] = value
            options.setdefault("base_url", OLLAMA_DEFAULT_BASE_URL)
        return cls(**options)

    return make


def _cli_factory(
    agent: AgentConfig, sandbox: SandboxConfig | None
) -> Callable[[], AgentAdapter]:
    """Build a zero-arg factory for a configured CLI adapter (cmd + sandbox)."""
    cls = _CLI_ADAPTER_CLASSES[agent.type]

    def make() -> AgentAdapter:
        if agent.cmd is not None:
            return cls(cmd=agent.cmd, sandbox=sandbox)
        return cls(sandbox=sandbox)

    return make


def _unimplemented_factory(agent_type: str) -> Callable[[], AgentAdapter]:
    """Make a factory for a configured but unknown agent type."""

    def make() -> AgentAdapter:
        known = ", ".join(sorted(KNOWN_AGENT_TYPES))
        raise AgentError(
            f"adapter '{agent_type}' is not a known agent type; known types: {known}"
        )

    return make


def build_registry(
    config: MakConfig, *, sandbox: SandboxConfig | None = None
) -> AdapterRegistry:
    """Register a config-bound adapter factory for every configured agent type.

    ``sandbox`` (when set) is threaded into CLI adapters so their subprocesses run
    inside a Docker container; API adapters ignore it (they make no subprocess).
    """
    if not config.agents:
        raise ConfigError("no agents configured; cannot build an adapter registry")
    registry = AdapterRegistry()
    for agent in config.agents:
        if agent.type in _API_ADAPTER_CLASSES:
            registry.register_factory(agent.type, _api_factory(agent))
        elif agent.type in _CLI_ADAPTER_CLASSES:
            registry.register_factory(agent.type, _cli_factory(agent, sandbox))
        else:
            registry.register_factory(agent.type, _unimplemented_factory(agent.type))
    return registry


def _check_local_options(agent: AgentConfig) -> None:
    """Reject a local-transport setting on a type that would ignore it.

    Every one of these is a startup ``ConfigError`` rather than a silent no-op
    mid-run: a setting written in good faith and quietly discarded is worse than
    a typo, because the run *looks* configured and behaves as if it were not.
    """
    if agent.base_url is not None and agent.type not in _LOCAL_TYPES:
        raise ConfigError(
            f"agent type '{agent.type}' ignores 'base_url'; it applies to "
            f"{', '.join(sorted(_LOCAL_TYPES))}"
        )
    if agent.type == "local_api" and agent.base_url is None:
        raise ConfigError(
            "agent type 'local_api' requires a 'base_url' naming the server "
            "(Ollama's OpenAI-compatible endpoint is "
            "http://localhost:11434/v1; vLLM and llama.cpp default to "
            "http://localhost:8000/v1 and http://localhost:8080/v1)"
        )
    for name in ("structured_output", "repair_attempts"):
        if getattr(agent, name) is not None and agent.type not in _LOCAL_TYPES:
            raise ConfigError(
                f"agent type '{agent.type}' ignores '{name}'; it applies to "
                f"{', '.join(sorted(_LOCAL_TYPES))}"
            )
    for name in _OLLAMA_ONLY_OPTIONS:
        if getattr(agent, name) is not None and agent.type != "ollama_api":
            raise ConfigError(
                f"agent type '{agent.type}' ignores '{name}'; it applies to "
                "ollama_api only"
            )


def validate_config(config: MakConfig) -> None:
    """Raise ``ConfigError`` if the configuration cannot be built as written.

    Catches a misspelled or unsupported ``type`` at startup instead of at dispatch
    time (where it would surface as a mid-run ``UnknownAgentTypeError``), and the
    same for a local-transport setting placed on a type that does not read it.
    """
    unknown = sorted({a.type for a in config.agents if a.type not in KNOWN_AGENT_TYPES})
    if unknown:
        known = ", ".join(sorted(KNOWN_AGENT_TYPES))
        raise ConfigError(
            f"unknown agent type(s) in config: {', '.join(unknown)}; "
            f"known types: {known}"
        )
    for agent in config.agents:
        _check_local_options(agent)


def default_agent_type(config: MakConfig) -> str:
    """Return the first configured agent: the default for bare tasks."""
    if not config.agents:
        raise ConfigError("no agents configured; cannot pick a default agent type")
    return config.agents[0].type


def healthy_agent_types(
    registry: AdapterRegistry, agent_types: list[str]
) -> tuple[list[str], list[str], dict[str, str]]:
    """Health-check each agent type once; return ``(healthy, unhealthy, why)``.

    Order is preserved. An adapter that cannot even be constructed (missing SDK,
    missing key) or whose ``health_check`` returns False (e.g. a CLI whose binary
    is not on PATH) is reported unhealthy — so a missing binary or key is caught
    at startup instead of as a mid-run task failure or a long timeout.

    ``why`` maps an unhealthy type to its adapter's own explanation, when the
    adapter offers one. An adapter opts in by exposing
    ``health_detail() -> str | None``; it is read with ``getattr``, so no adapter
    is forced to implement it. The generic warning ("missing API key/SDK, or CLI
    not on PATH") is never the reason a local server is unreachable, and telling
    a user that when the truth is "Ollama is not running" or "that model is not
    pulled" sends them to fix the wrong thing.
    """
    healthy: list[str] = []
    unhealthy: list[str] = []
    why: dict[str, str] = {}
    for agent_type in agent_types:
        adapter: AgentAdapter | None = None
        try:
            adapter = registry.get(agent_type)
            ok = adapter.health_check()
        except Exception as exc:
            ok = False
            why[agent_type] = str(exc)
        if ok:
            healthy.append(agent_type)
            continue
        unhealthy.append(agent_type)
        detail = _health_detail(adapter)
        if detail:
            why[agent_type] = detail
    return healthy, unhealthy, why


def _health_detail(adapter: AgentAdapter | None) -> str | None:
    """Return an adapter's own reason for failing its health check, if any."""
    if adapter is None:
        return None
    describe = getattr(adapter, "health_detail", None)
    if not callable(describe):
        return None
    try:
        detail = describe()
    except Exception:
        return None
    return detail if isinstance(detail, str) and detail else None
