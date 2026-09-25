"""Resolve a configured roster into ``ResolvedAgentConfig`` records.

This is the single place that turns "what the user wrote" into "what the kernel
routes on", for both schema generations at once:

* a legacy ``{type: openai_api, model: …}`` entry gets a synthesized built-in
  endpoint (``mak.endpoints.builtin``) and derives its id from its type, which
  reproduces today's behaviour exactly;
* a canonical ``{id: …, endpoint: …, model: …}`` entry resolves against the
  merged endpoint set.

Kept out of ``mak.endpoints.resolution`` because that module is deliberately
free of any ``mak.config`` import; this one needs ``AgentConfig`` and therefore
sits one layer up.
"""

from __future__ import annotations

from collections.abc import Mapping

from mak.config import AgentConfig, MakConfig
from mak.core.exceptions import ConfigError
from mak.endpoints.builtin import (
    CLI_AGENT_TYPES,
    builtin_endpoint_for,
    is_endpoint_backed_type,
)
from mak.endpoints.resolution import (
    ResolvedAgentConfig,
    ResolvedEndpoint,
    require_endpoint,
    resolve_endpoint,
)
from mak.endpoints.types import validate_agent_id


def resolve_agents(
    config: MakConfig,
    *,
    endpoints: dict[str, ResolvedEndpoint] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[ResolvedAgentConfig, ...]:
    """Resolve every configured agent, rejecting duplicate routing ids.

    ``endpoints`` is the **merged** set — project config plus the user store —
    already resolved. It is passed in rather than loaded here so the composition
    root decides what a run can see, and tests can supply an explicit set.

    A duplicate id is rejected here rather than at registration so the message
    can name the configuration rather than the registry, and so the check runs
    even for callers that never build a registry.
    """
    available = endpoints or {}
    resolved: list[ResolvedAgentConfig] = []
    seen: dict[str, str] = {}
    for agent in config.agents:
        item = _resolve_one(agent, available, env)
        if item.id in seen:
            raise ConfigError(
                f"two agents resolve to the id '{item.id}'. Ids must be unique "
                "because the scheduler, planner, logs and git metadata route by "
                "them — give one of them an explicit 'id'."
            )
        seen[item.id] = agent.type
        resolved.append(item)
    return tuple(resolved)


def _resolve_one(
    agent: AgentConfig,
    endpoints: dict[str, ResolvedEndpoint],
    env: Mapping[str, str] | None,
) -> ResolvedAgentConfig:
    """Resolve one agent entry, legacy or endpoint-backed."""
    if agent.endpoint:
        endpoint = require_endpoint(
            endpoints, agent.endpoint, where=f"agent '{agent.routing_id()}'"
        )
        agent_id = validate_agent_id(agent.id or _derived_id(agent, endpoint))
        return _build(agent, agent_id, endpoint.adapter_type, endpoint)

    if agent.type in CLI_AGENT_TYPES:
        return _build(agent, _legacy_id(agent), agent.type, None)

    if is_endpoint_backed_type(agent.type):
        endpoint = resolve_endpoint(builtin_endpoint_for(agent), env=env)
        return _build(agent, _legacy_id(agent), agent.type, endpoint)

    # An unknown type still resolves: ``mak.bootstrap`` registers a factory that
    # raises a message naming the known types, so the failure stays a clear
    # startup error rather than a KeyError here.
    return _build(agent, _legacy_id(agent), agent.type, None)


def _legacy_id(agent: AgentConfig) -> str:
    """Return the routing id for a type-based entry.

    Unset, it is the type itself — which is precisely the key the registry used
    before Wave 22, so every pre-existing config routes exactly as it did.
    """
    return validate_agent_id(agent.id) if agent.id else agent.type


def _derived_id(agent: AgentConfig, endpoint: ResolvedEndpoint) -> str:
    """Return a derived id for an endpoint-backed entry with no explicit one."""
    if not agent.model:
        return endpoint.id
    return derive_agent_id(endpoint.id, agent.model)


def _build(
    agent: AgentConfig,
    agent_id: str,
    adapter_type: str,
    endpoint: ResolvedEndpoint | None,
) -> ResolvedAgentConfig:
    """Assemble the resolved record, carrying the agent's generation limits."""
    return ResolvedAgentConfig(
        id=agent_id,
        adapter_type=adapter_type,
        endpoint=endpoint,
        model=agent.model,
        max_instances=agent.max_instances,
        timeout=agent.timeout,
        max_tokens=agent.max_tokens,
        structured_output=agent.structured_output,
        repair_attempts=agent.repair_attempts,
        num_ctx=agent.num_ctx,
        keep_alive=agent.keep_alive,
        temperature=agent.temperature,
        cmd=agent.cmd,
    )


# How much of a model id survives into a derived agent id. Long enough to stay
# readable in a log line and a git trailer, short enough not to dominate them.
_MODEL_SLUG_LIMIT = 48


def derive_agent_id(endpoint_id: str, model: str) -> str:
    """Return the deterministic agent id for an endpoint/model pairing.

    ``nvidia`` + ``meta/llama-3.3-70b-instruct`` becomes
    ``nvidia-meta-llama-3-3-70b-instruct``. Deterministic because a CLI spec
    must name the same agent on every run for recovery files, pool caps and log
    correlation to line up; readable because this string appears in git commit
    trailers a human reads later.
    """
    slug: list[str] = []
    for char in model.lower():
        if char.isascii() and char.isalnum():
            slug.append(char)
        elif slug and slug[-1] != "-":
            slug.append("-")
    text = "".join(slug).strip("-")[:_MODEL_SLUG_LIMIT].strip("-")
    return f"{endpoint_id}-{text}" if text else endpoint_id


def unique_agent_id(candidate: str, taken: set[str]) -> str:
    """Return ``candidate``, or the first free ``candidate-N`` suffix.

    Two models whose ids differ only in characters the slug drops would collide;
    a numeric suffix keeps both usable rather than failing a command line the
    user has every reason to expect to work.
    """
    if candidate not in taken:
        return candidate
    index = 2
    while f"{candidate}-{index}" in taken:
        index += 1
    return f"{candidate}-{index}"
