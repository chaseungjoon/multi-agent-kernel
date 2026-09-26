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
from collections.abc import Callable, Iterable, Mapping
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
from mak.config import AgentConfig, MakConfig, PlannerConfig, normalize_base_url
from mak.core.exceptions import AgentError, ConfigError
from mak.endpoints.agents import resolve_agents
from mak.endpoints.capabilities import CapabilityCache
from mak.endpoints.resolution import ResolvedAgentConfig, resolve_endpoints
from mak.endpoints.store import load_user_endpoints, merge_endpoints
from mak.local.discovery import LOCAL_BASE_URL_ENV
from mak.local.ollama_client import DEFAULT_BASE_URL as OLLAMA_DEFAULT_BASE_URL
from mak.models.registry import ReportedCapabilities

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

# Hosted provider -> the planner backend ``build_planner_llm`` names it by.
_PROVIDER_TO_PLANNER_BACKEND: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "gemini",
    "google": "gemini",
}

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


def _local_agent(
    provider: str,
    model: str,
    url: str,
    spec: str,
    env: Mapping[str, str] | None = None,
) -> AgentConfig:
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
    source = os.environ if env is None else env
    base_url = url or source.get(LOCAL_BASE_URL_ENV, "").strip()
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


def planner_from_spec(
    spec: str,
    planner: PlannerConfig,
    *,
    env: Mapping[str, str] | None = None,
    endpoint_ids: Iterable[str] | None = None,
) -> PlannerConfig:
    """Point ``planner`` at a ``provider:model[@base_url]`` spec.

    A compatibility shell over :class:`mak.application.route.PlannerRoute`,
    which owns the grammar: parsing a spec and applying a route are one
    implementation whichever front end asks.
    """
    from mak.application.route import PlannerRoute

    route = PlannerRoute.from_spec(spec, endpoint_ids=endpoint_ids, env=env)
    return route.apply(planner)


def agents_from_specs(
    specs: list[str],
    *,
    env: Mapping[str, str] | None = None,
    endpoint_ids: Iterable[str] | None = None,
) -> tuple[AgentConfig, ...]:
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

    A **configured endpoint id** is accepted in the provider position and is
    tried first: ``--models nvidia:meta/llama-3.3-70b-instruct`` resolves
    against the endpoints in the project config and the user store. Reserved ids can
    never be taken by a user endpoint, so a prefix has exactly one meaning.

    Several models on one endpoint, and several endpoints on one transport, are
    both legal — uniqueness is by **agent id**, not by provider. Keying the
    registry by adapter type instead would let a second OpenAI-compatible entry
    silently replace the first.

    ``env`` is the environment an ``ollama:``/``local:`` spec without a URL
    reads its default endpoint from (default: the process environment);
    ``endpoint_ids`` the endpoints a prefix may name (default: every one
    :func:`configured_endpoint_ids` finds).
    """
    if not specs:
        raise ConfigError(f"--models needs at least one entry: {_SPEC_SYNTAX}")
    endpoints = (
        frozenset(endpoint_ids)
        if endpoint_ids is not None
        else configured_endpoint_ids()
    )
    agents: list[AgentConfig] = []
    taken: set[str] = set()
    for spec in specs:
        provider, model, url = _split_spec(spec)
        if provider in endpoints:
            agent = _endpoint_agent(provider, model, url, spec, taken)
        elif provider in _PROVIDER_TO_API or provider in _PROVIDER_TO_LOCAL:
            agent = _legacy_agent(provider, model, url, spec, env)
        else:
            known = ", ".join(sorted({*SUPPORTED_PROVIDERS, *endpoints}))
            raise ConfigError(
                f"unknown endpoint or provider {provider!r}; MAK knows "
                f"{known} — write {_SPEC_SYNTAX}, or add an endpoint with "
                "'/endpoint add' in the interactive CLI"
            )
        agent_id = agent.routing_id()
        if agent_id in taken:
            raise ConfigError(
                f"{spec!r} resolves to the agent id '{agent_id}', which another "
                "entry already claims. Two models on one endpoint need distinct "
                "ids — name them in your config file with an explicit 'id'."
            )
        taken.add(agent_id)
        agents.append(agent)
    return tuple(agents)


def configured_endpoint_ids(config: MakConfig | None = None) -> frozenset[str]:
    """Return the ids of every configured endpoint, or an empty set.

    The user store's endpoints plus ``config``'s own; with no ``config``, the
    one :func:`~mak.config.discover_config_path` finds from the current
    directory. A caller holding the run's config passes it, so a project
    config discovered from a work dir other than the CWD is the one consulted.

    Total: ``--models`` has to keep working when the endpoint store is
    unreadable, and the store's own diagnostic is surfaced by ``/endpoint``.
    """
    try:
        from mak.endpoints.store import load_user_endpoints

        saved, _diagnostic = load_user_endpoints()
    except Exception:  # noqa: BLE001 - a broken store must not break the CLI
        return frozenset()
    ids = {e.id for e in saved}
    if config is not None:
        return frozenset(ids | {e.id for e in config.endpoints})
    try:
        from mak.config import discover_config_path, load_config

        ids |= {e.id for e in load_config(discover_config_path()).endpoints}
    except Exception:  # noqa: BLE001 - the config reports its own problems
        pass
    return frozenset(ids)


def _endpoint_agent(
    endpoint_id: str, model: str, url: str, spec: str, taken: set[str]
) -> AgentConfig:
    """Build the roster entry for an ``<endpoint>:<model>`` spec."""
    from mak.endpoints.agents import derive_agent_id, unique_agent_id

    if url:
        raise ConfigError(
            f"{spec!r} names endpoint {endpoint_id!r} and also an '@<base_url>'; "
            "the endpoint already has an address. Edit it with "
            f"'/endpoint edit {endpoint_id}' to change where it points."
        )
    if not model:
        raise ConfigError(
            f"{spec!r} names no model; write {endpoint_id}:<model> — "
            f"'/endpoint models {endpoint_id}' lists what it offers"
        )
    return AgentConfig(
        # Filled from the endpoint's transport during resolution.
        type="",
        id=unique_agent_id(derive_agent_id(endpoint_id, model), taken),
        endpoint=endpoint_id,
        model=model,
    )


def _legacy_agent(
    provider: str,
    model: str,
    url: str,
    spec: str,
    env: Mapping[str, str] | None = None,
) -> AgentConfig:
    """Build the roster entry for a legacy ``provider[:model][@url]`` spec."""
    if url and provider not in _BASE_URL_PROVIDERS:
        raise ConfigError(
            f"provider {provider!r} does not take an '@<base_url>'; only "
            f"{', '.join(sorted(_BASE_URL_PROVIDERS))} do"
        )
    if provider in _PROVIDER_TO_LOCAL:
        return _local_agent(provider, model, url, spec, env)
    agent_type, key_env = _PROVIDER_TO_API[provider]
    return AgentConfig(
        type=agent_type,
        model=model or None,
        api_key_env=key_env,
        base_url=(
            normalize_base_url(url, where=f"--models entry {spec!r}")
            if url
            else None
        ),
    )


def _api_factory(
    agent: ResolvedAgentConfig, capabilities: CapabilityCache | None = None
) -> Callable[[], AgentAdapter]:
    """Build a zero-arg factory for a configured API adapter (lazy SDK client).

    Every option comes from the **resolved** agent, so the endpoint's decided
    capabilities — not a re-derivation of them here — are what the adapter is
    constructed with.

    ``capabilities`` is the session's shared capability cache. The registry
    builds a fresh adapter per dispatch, so the memory of what an endpoint
    actually supports cannot live on the instance; closing the factory over one
    explicitly-owned object is how it survives without a module global.
    """
    cls = _API_ADAPTER_CLASSES[agent.adapter_type]
    endpoint = agent.endpoint

    def make() -> AgentAdapter:
        # Each unset option is *omitted* rather than passed as None, so the
        # adapter's own default (including "resolve the budget from the model
        # catalog") stays the single place that decides it.
        options: dict[str, Any] = {
            "api_key": endpoint.api_key if endpoint else None,
        }
        if agent.model is not None:
            options["model"] = agent.model
        if agent.max_tokens is not None:
            options["max_tokens"] = agent.max_tokens
        # ``timeout`` was previously consumed only by the *subprocess* read loop,
        # so an API agent had no bound at all: a wedged provider call never
        # returned and the session hung shutting its pool down. The configured
        # per-agent value now reaches the SDK client that actually makes the call.
        options["timeout"] = float(agent.timeout)
        # ``agent_id`` is the routing key the kernel uses; ``agent_type`` stays
        # as transport/telemetry, so a log line still says which wire protocol
        # spoke even when two agents share it.
        options["agent_id"] = agent.id
        # Each new option reaches **only** the types whose constructor accepts
        # it: the Anthropic and Gemini adapters take none of them, so an
        # unconditional kwarg would be a TypeError at dispatch time.
        if agent.adapter_type in _LOCAL_TYPES:
            options["agent_type"] = agent.adapter_type
            if endpoint is not None and endpoint.base_url is not None:
                options["base_url"] = endpoint.base_url
            for name in ("structured_output", "repair_attempts"):
                value = getattr(agent, name, None)
                if value is not None:
                    options[name] = value
            if "structured_output" not in options and endpoint is not None:
                options["structured_output"] = endpoint.structured_output.value
        if agent.adapter_type in _OPENAI_COMPATIBLE_TYPES and endpoint is not None:
            # Capability decisions the adapter must not re-derive. The token
            # field name in particular used to be guessed from whether a
            # base_url was set, which is wrong for every hosted compatible
            # service.
            options["token_parameter"] = endpoint.token_parameter.value
            options["provider_routing"] = endpoint.provider_routing.value
            options["headers"] = endpoint.headers
            options["endpoint_id"] = endpoint.id
            options["endpoint_name"] = endpoint.display_name
            options["health_check_policy"] = endpoint.health_check.value
            options["chat_probe_ok"] = endpoint.chat_probe_allowed
            options["api_key_env"] = endpoint.api_key_env
            if capabilities is not None:
                options["capabilities"] = capabilities
        if agent.adapter_type == "ollama_api":
            for name in _OLLAMA_ONLY_OPTIONS:
                value = getattr(agent, name)
                if value is not None:
                    options[name] = value
            options.setdefault("base_url", OLLAMA_DEFAULT_BASE_URL)
        return cls(**options)

    return make


def _cli_factory(
    agent: ResolvedAgentConfig, sandbox: SandboxConfig | None
) -> Callable[[], AgentAdapter]:
    """Build a zero-arg factory for a configured CLI adapter (cmd + sandbox)."""
    cls = _CLI_ADAPTER_CLASSES[agent.adapter_type]

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


def resolved_agents(
    config: MakConfig, *, env: Mapping[str, str] | None = None
) -> tuple[ResolvedAgentConfig, ...]:
    """Resolve the roster, merging project endpoints with the user store.

    The single place a run decides which endpoints exist. Both the registry and
    every caller that needs to know an agent's model or endpoint go through
    here, so no two of them can disagree about what is configured.
    """
    user_endpoints, _diagnostic = load_user_endpoints()
    merged = merge_endpoints(config.endpoints, user_endpoints)
    return resolve_agents(
        config, endpoints=resolve_endpoints(merged, env=env), env=env
    )


def seed_capabilities(
    cache: CapabilityCache,
    roster: tuple[ResolvedAgentConfig, ...],
    reported: ReportedCapabilities,
) -> None:
    """Seed the session's capability cache from the model catalog.

    Called once, before the first dispatch, so a capability the endpoint has
    *already published* is honored without paying a rejected request to
    rediscover it. Without it, each task can cost one failed provider
    call for a fact that was sitting in OpenRouter's
    ``/models`` response the whole time.

    Only the reported parameter set is seeded — never a mode. Choosing the rung
    from it is the adapter's job, because the adapter also knows the user's
    configured ceiling and must never be pushed above it.

    Pairs the catalog knows nothing about are skipped rather than recorded as
    empty: "unknown" and "reported nothing" are different facts, and recording
    the wrong one would disable structured output for every endpoint whose
    ``/models`` route returns bare ids.
    """
    for agent in roster:
        if agent.endpoint is None or agent.model is None:
            continue
        parameters = reported.for_model(agent.endpoint.id, agent.model)
        if parameters is None:
            continue
        cache.record_reported_parameters(
            agent.endpoint.id, agent.model, parameters
        )


def build_registry(
    config: MakConfig,
    *,
    sandbox: SandboxConfig | None = None,
    agents: tuple[ResolvedAgentConfig, ...] | None = None,
    reported: ReportedCapabilities | None = None,
) -> AdapterRegistry:
    """Register a config-bound adapter factory for every configured agent.

    Keyed by **agent id**, so two agents backed by the same adapter class — two
    OpenAI-compatible endpoints, say — both survive registration instead of the
    second silently replacing the first.

    ``sandbox`` (when set) is threaded into CLI adapters so their subprocesses run
    inside a Docker container; API adapters ignore it (they make no subprocess).

    ``agents`` accepts an already-resolved roster so a caller that has one (the
    composition root does) does not resolve twice.

    ``reported`` is the model catalog's published capability data, injected
    rather than read from disk here: this function stays pure, and a test can
    state exactly what the catalog says without writing a manifest. Omitting it
    means no seeding, which is the historical behavior — every pair is
    discovered at runtime.
    """
    if not config.agents:
        raise ConfigError("no agents configured; cannot build an adapter registry")
    roster = agents if agents is not None else resolved_agents(config)
    registry = AdapterRegistry()
    # One cache per registry, so everything this run dispatches shares what it
    # learns and two sessions in one process never do.
    capabilities = CapabilityCache()
    if reported is not None:
        seed_capabilities(capabilities, roster, reported)
    for agent in roster:
        if agent.adapter_type in _API_ADAPTER_CLASSES:
            registry.register_factory(agent.id, _api_factory(agent, capabilities))
        elif agent.adapter_type in _CLI_ADAPTER_CLASSES:
            registry.register_factory(agent.id, _cli_factory(agent, sandbox))
        else:
            registry.register_factory(
                agent.id, _unimplemented_factory(agent.adapter_type)
            )
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
    # An endpoint-backed entry has no ``type`` of its own — the endpoint's
    # transport supplies it — so it is exempt from the type check and from the
    # local-option rules, which exist to catch a setting placed on a type that
    # would ignore it.
    legacy = [a for a in config.agents if not a.endpoint]
    unknown = sorted({a.type for a in legacy if a.type not in KNOWN_AGENT_TYPES})
    if unknown:
        known = ", ".join(sorted(KNOWN_AGENT_TYPES))
        raise ConfigError(
            f"unknown agent type(s) in config: {', '.join(unknown)}; "
            f"known types: {known}"
        )
    for agent in legacy:
        _check_local_options(agent)
    _check_adjudicator(config)


def _check_adjudicator(config: MakConfig) -> None:
    """Refuse an adjudicator the stale-read policy would never consult.

    The adjudicator is only asked about the stale reads ``revalidate`` cannot
    settle; every other policy decides without it. Configuring one there would
    look like a safety net that does nothing, so it is rejected at startup.
    """
    semantic = config.semantic
    if semantic.adjudicator is not None and semantic.stale_read != "revalidate":
        raise ConfigError(
            f"semantic.adjudicator is set but semantic.stale_read is "
            f"'{semantic.stale_read}', which never consults it; use "
            "stale_read: revalidate, or turn the adjudicator off"
        )


def default_agent_id(config: MakConfig) -> str:
    """Return the first configured agent's routing id: the default for bare tasks.

    For a legacy roster the id *is* the type, so this returns exactly what
    ``default_agent_type`` always did.
    """
    if not config.agents:
        raise ConfigError("no agents configured; cannot pick a default agent")
    return config.agents[0].routing_id()


def default_agent_type(config: MakConfig) -> str:
    """Return the default agent — deprecated alias of :func:`default_agent_id`."""
    return default_agent_id(config)


def healthy_agent_ids(
    registry: AdapterRegistry, agent_ids: list[str]
) -> tuple[list[str], list[str], dict[str, str]]:
    """Health-check each agent once; return ``(healthy, unhealthy, why)``.

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
    for agent_id in agent_ids:
        adapter: AgentAdapter | None = None
        try:
            adapter = registry.get(agent_id)
            ok = adapter.health_check()
        except Exception as exc:
            ok = False
            why[agent_id] = str(exc)
        if ok:
            healthy.append(agent_id)
            continue
        unhealthy.append(agent_id)
        detail = _health_detail(adapter)
        if detail:
            why[agent_id] = detail
    return healthy, unhealthy, why


def healthy_agent_types(
    registry: AdapterRegistry, agent_types: list[str]
) -> tuple[list[str], list[str], dict[str, str]]:
    """Health-check each agent — deprecated alias of :func:`healthy_agent_ids`."""
    return healthy_agent_ids(registry, agent_types)


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
