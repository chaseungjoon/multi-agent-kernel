"""Adapter registry: register and look up agent adapters by type.

``AdapterRegistry`` is an instance, not module-global mutable state (AGENTS.md):
the kernel owns one registry and passes it explicitly, so tests and concurrent
sessions never share a hidden dict.

Adapters can be registered two ways:

- ``register(agent_type, cls)`` — a zero-arg adapter class, instantiated on
  ``get``. Convenient for tests and adapters that need no configuration.
- ``register_factory(agent_type, factory)`` — a callable returning an adapter
  instance. This is the seam the composition root (``mak/bootstrap.py``) uses to
  bind a configured ``model`` / API key into an adapter, which a bare class
  cannot carry through zero-arg instantiation.
"""

from __future__ import annotations

from collections.abc import Callable

from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.core.exceptions import UnknownAgentTypeError

AdapterFactory = Callable[[], AgentAdapter]


class AdapterRegistry:
    """A collection of agent adapter factories keyed by agent type."""

    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, agent_type: str, adapter_cls: type[AgentAdapter]) -> None:
        """Register a zero-arg adapter class for a given agent type."""
        self._factories[agent_type] = adapter_cls

    def register_factory(
        self, agent_type: str, factory: AdapterFactory
    ) -> None:
        """Register a factory that builds (possibly configured) adapter instances."""
        self._factories[agent_type] = factory

    def get(self, agent_type: str) -> AgentAdapter:
        """Look up and instantiate an adapter by agent type."""
        if agent_type not in self._factories:
            raise UnknownAgentTypeError(f"no adapter registered for '{agent_type}'")
        return self._factories[agent_type]()

    def list_types(self) -> list[str]:
        """Return all registered agent type names."""
        return list(self._factories)

    def clear(self) -> None:
        """Remove all registered adapters."""
        self._factories.clear()
