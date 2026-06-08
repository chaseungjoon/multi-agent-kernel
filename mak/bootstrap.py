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
from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter
from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.agent_runner.registry import AdapterRegistry
from mak.config import AgentConfig, MakConfig
from mak.core.exceptions import AgentError, ConfigError

# Agent types with a built, first-party API adapter — each takes ``model`` +
# ``api_key`` kwargs. Other (e.g. CLI) types parse and validate here but resolve
# to a clear "not yet implemented" error rather than crashing wiring.
# ``Callable[..., AgentAdapter]`` keeps the generic factory below honest without
# naming each constructor's full (and differing) signature.
_API_ADAPTER_CLASSES: dict[str, Callable[..., AgentAdapter]] = {
    "anthropic_api": AnthropicApiAdapter,
    "openai_api": OpenAiApiAdapter,
    "gemini_api": GeminiApiAdapter,
}


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


def _unimplemented_factory(agent_type: str) -> Callable[[], AgentAdapter]:
    """Make a factory for a configured but not-yet-built agent type."""

    def make() -> AgentAdapter:
        raise AgentError(
            f"adapter '{agent_type}' is configured but not yet implemented; "
            "use 'anthropic_api', 'openai_api', or 'gemini_api'"
        )

    return make


def build_registry(config: MakConfig) -> AdapterRegistry:
    """Register a config-bound adapter factory for every configured agent type."""
    if not config.agents:
        raise ConfigError("no agents configured; cannot build an adapter registry")
    registry = AdapterRegistry()
    for agent in config.agents:
        if agent.type in _API_ADAPTER_CLASSES:
            registry.register_factory(agent.type, _api_factory(agent))
        else:
            registry.register_factory(
                agent.type, _unimplemented_factory(agent.type)
            )
    return registry


def default_agent_type(config: MakConfig) -> str:
    """Return the first configured agent: the default for bare tasks."""
    if not config.agents:
        raise ConfigError("no agents configured; cannot pick a default agent type")
    return config.agents[0].type
