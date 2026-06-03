"""Adapter registry: register and look up agent adapters by type.

``AdapterRegistry`` is an instance, not module-global mutable state (AGENTS.md):
the kernel owns one registry and passes it explicitly, so tests and concurrent
sessions never share a hidden dict.
"""

from __future__ import annotations

from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.core.exceptions import UnknownAgentTypeError


class AdapterRegistry:
    """A collection of agent adapters keyed by agent type."""

    def __init__(self) -> None:
        self._adapters: dict[str, type[AgentAdapter]] = {}

    def register(self, agent_type: str, adapter_cls: type[AgentAdapter]) -> None:
        """Register an adapter class for a given agent type."""
        self._adapters[agent_type] = adapter_cls

    def get(self, agent_type: str) -> AgentAdapter:
        """Look up and instantiate an adapter by agent type."""
        if agent_type not in self._adapters:
            raise UnknownAgentTypeError(f"no adapter registered for '{agent_type}'")
        return self._adapters[agent_type]()

    def list_types(self) -> list[str]:
        """Return all registered agent type names."""
        return list(self._adapters)

    def clear(self) -> None:
        """Remove all registered adapters."""
        self._adapters.clear()
