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

from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.agent_runner.adapters.claude_code_adapter import ClaudeCodeAdapter
from mak.agent_runner.adapters.codex_adapter import CodexAdapter
from mak.agent_runner.adapters.copilot_adapter import CopilotAdapter
from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter
from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.agent_runner.registry import AdapterRegistry
from mak.agent_runner.sandbox import SandboxConfig
from mak.config import AgentConfig, MakConfig
from mak.core.exceptions import AgentError, ConfigError

# Agent types with a built, first-party API adapter — each takes ``model`` +
# ``api_key`` kwargs. ``Callable[..., AgentAdapter]`` keeps the generic factory
# below honest without naming each constructor's full (and differing) signature.
_API_ADAPTER_CLASSES: dict[str, Callable[..., AgentAdapter]] = {
    "anthropic_api": AnthropicApiAdapter,
    "openai_api": OpenAiApiAdapter,
    "gemini_api": GeminiApiAdapter,
}

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
        if agent.model is not None:
            return cls(model=agent.model, api_key=key)
        return cls(api_key=key)

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


def validate_config(config: MakConfig) -> None:
    """Raise ``ConfigError`` if any agent names a type the kernel cannot build.

    Catches a misspelled or unsupported ``type`` at startup instead of at dispatch
    time (where it would surface as a mid-run ``UnknownAgentTypeError``).
    """
    unknown = sorted({a.type for a in config.agents if a.type not in KNOWN_AGENT_TYPES})
    if unknown:
        known = ", ".join(sorted(KNOWN_AGENT_TYPES))
        raise ConfigError(
            f"unknown agent type(s) in config: {', '.join(unknown)}; "
            f"known types: {known}"
        )


def default_agent_type(config: MakConfig) -> str:
    """Return the first configured agent: the default for bare tasks."""
    if not config.agents:
        raise ConfigError("no agents configured; cannot pick a default agent type")
    return config.agents[0].type
