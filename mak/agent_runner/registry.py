"""Adapter registry: register and look up agent adapters by type."""

from __future__ import annotations

from mak.core.exceptions import UnknownAgentTypeError
from mak.agent_runner.adapters.base_adapter import AgentAdapter

ADAPTER_REGISTRY: dict[str, type[AgentAdapter]] = {}


def register_adapter(agent_type: str, adapter_cls: type[AgentAdapter]) -> None:
    """Register an adapter class for a given agent type."""
    ADAPTER_REGISTRY[agent_type] = adapter_cls


def get_adapter(agent_type: str) -> AgentAdapter:
    """Look up and instantiate an adapter by agent type."""
    if agent_type not in ADAPTER_REGISTRY:
        raise UnknownAgentTypeError(
            f"no adapter registered for '{agent_type}'"
        )
    return ADAPTER_REGISTRY[agent_type]()


def list_adapters() -> list[str]:
    """Return all registered agent type names."""
    return list(ADAPTER_REGISTRY.keys())


def clear_registry() -> None:
    """Remove all registered adapters (useful for testing)."""
    ADAPTER_REGISTRY.clear()
