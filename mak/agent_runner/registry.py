"""Adapter registry: register and look up agent adapters by **agent id**.

``AdapterRegistry`` is an instance, not module-global mutable state (AGENTS.md):
the kernel owns one registry and passes it explicitly, so tests and concurrent
sessions never share a hidden dict.

**Keyed by agent id, not adapter type.** The key used to be the agent
*type*, which meant a roster naming two OpenAI-compatible endpoints — cloud
OpenAI and NVIDIA, say — registered both under ``openai_api`` and the second
silently replaced the first. One of the two endpoints simply never ran, and
nothing downstream could tell, because there was only ever one entry to look at.
The key is now the configured agent id, so an adapter *class* may back any
number of registered agents.

A duplicate id is therefore a hard error rather than an overwrite. Registration
happens once per run from a validated config, so a collision means two agents
genuinely claim one identity — and silently dropping one of them is the exact
bug this change exists to remove.

Adapters can be registered two ways:

- ``register(agent_id, cls)`` — a zero-arg adapter class, instantiated on
  ``get``. Convenient for tests and adapters that need no configuration.
- ``register_factory(agent_id, factory)`` — a callable returning an adapter
  instance. This is the seam the composition root (``mak/bootstrap.py``) uses to
  bind a configured ``model``, endpoint and API key into an adapter, which a
  bare class cannot carry through zero-arg instantiation.
"""

from __future__ import annotations

from collections.abc import Callable

from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.core.exceptions import ConfigError, UnknownAgentTypeError

AdapterFactory = Callable[[], AgentAdapter]


class AdapterRegistry:
    """A collection of agent adapter factories keyed by agent id."""

    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, agent_id: str, adapter_cls: type[AgentAdapter]) -> None:
        """Register a zero-arg adapter class under ``agent_id``."""
        self._claim(agent_id)
        self._factories[agent_id] = adapter_cls

    def register_factory(self, agent_id: str, factory: AdapterFactory) -> None:
        """Register a factory that builds (possibly configured) adapters."""
        self._claim(agent_id)
        self._factories[agent_id] = factory

    def replace_factory(self, agent_id: str, factory: AdapterFactory) -> None:
        """Substitute the factory for an **already registered** agent id.

        The deliberate counterpart to :meth:`register_factory`'s refusal.
        Swapping a configured adapter for a double — a stopped server, an
        unpulled model — is a real thing callers and tests do, and it needs a
        door of its own precisely so that the *accidental* overwrite stays an
        error. Registering an unknown id this way is refused, because a typo
        there would silently create an agent nothing dispatches to.
        """
        if agent_id not in self._factories:
            raise UnknownAgentTypeError(
                f"cannot replace '{agent_id}': no adapter is registered under "
                "that id; use register_factory to add a new one"
            )
        self._factories[agent_id] = factory

    def _claim(self, agent_id: str) -> None:
        """Reserve ``agent_id``, refusing a second claim on the same name.

        The refusal is the point: a ``dict`` assignment here used to discard a
        configured agent without a word, which is how a roster naming two
        OpenAI-compatible endpoints silently ran only one of them.
        """
        if agent_id in self._factories:
            raise ConfigError(
                f"two agents are configured with the id '{agent_id}'; ids must "
                "be unique because the scheduler, the planner and the logs all "
                "route by them. Give each agent its own 'id'."
            )

    def get(self, agent_id: str) -> AgentAdapter:
        """Look up and instantiate an adapter by agent id."""
        if agent_id not in self._factories:
            raise UnknownAgentTypeError(f"no adapter registered for '{agent_id}'")
        return self._factories[agent_id]()

    def list_ids(self) -> list[str]:
        """Return every registered agent id, in registration order."""
        return list(self._factories)

    def list_types(self) -> list[str]:
        """Return every registered agent id — deprecated alias of :meth:`list_ids`.

        Retained because ``Session`` discovers it with ``getattr`` and an
        external caller may too. Removed once no caller uses the old name.
        """
        return self.list_ids()

    def clear(self) -> None:
        """Remove all registered adapters."""
        self._factories.clear()
