"""Synthesize endpoints for MAK's built-in providers and legacy agent types.

Wave 22 gives every agent a resolved endpoint so that one code path serves both
``{type: openai_api}`` written in 2025 and ``{endpoint: nvidia}`` written today.
The legacy types are not special-cased downstream; they are simply agents whose
endpoint MAK supplies.

The ids here are exactly the provider prefixes ``--models`` has always accepted
(``mak.endpoints.profiles.RESERVED_ENDPOINT_IDS``), which is why those ids are
reserved: ``--models openai:gpt-5.6-sol`` and ``--models nvidia:llama`` then
resolve through one rule instead of two, and a user endpoint cannot quietly
take over a prefix that already means something.

A synthesized endpoint honours whatever the agent stated. ``openai_api`` with a
``base_url`` is a gateway or proxy and keeps today's behaviour, including the
rule that a real ``OPENAI_API_KEY`` is never forwarded to it unless the config
named that variable itself.
"""

from __future__ import annotations

from mak.config import AgentConfig
from mak.core.exceptions import ConfigError
from mak.endpoints.types import EndpointConfig, Location, Transport

# Legacy agent type -> (endpoint id, transport, conventional key env, location).
# ``None`` for the key env means "this transport has no conventional credential"
# — a local runtime has none by construction, and that absence is what lets the
# placeholder-key rule apply.
_BUILTIN_BY_TYPE: dict[str, tuple[str, Transport, str | None, Location]] = {
    "anthropic_api": (
        "anthropic",
        Transport.ANTHROPIC,
        "ANTHROPIC_API_KEY",
        Location.HOSTED,
    ),
    "openai_api": ("openai", Transport.OPENAI_CHAT, "OPENAI_API_KEY", Location.HOSTED),
    "gemini_api": ("gemini", Transport.GEMINI, "GEMINI_API_KEY", Location.HOSTED),
    "local_api": ("local", Transport.OPENAI_CHAT, None, Location.LOCAL),
    "ollama_api": ("ollama", Transport.OLLAMA_NATIVE, None, Location.LOCAL),
}

# Display names for the built-in endpoints, so status lines and the planner
# prompt read the same way for a built-in and a user-created endpoint.
_BUILTIN_DISPLAY: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Google Gemini",
    "local": "Local OpenAI-compatible server",
    "ollama": "Ollama",
}

# Agent types driven by a local CLI binary rather than an endpoint.
CLI_AGENT_TYPES: frozenset[str] = frozenset({"claude_code", "codex", "copilot"})


def is_endpoint_backed_type(agent_type: str) -> bool:
    """Return whether this legacy agent type resolves through an endpoint."""
    return agent_type in _BUILTIN_BY_TYPE


def builtin_endpoint_for(agent: AgentConfig) -> EndpointConfig:
    """Return the synthesized endpoint for a legacy, type-based agent entry.

    The agent's own ``base_url`` and ``api_key_env`` win over the built-in
    defaults, because those fields are exactly how a user points a legacy entry
    somewhere other than the provider's own host.
    """
    try:
        endpoint_id, transport, default_key_env, location = _BUILTIN_BY_TYPE[
            agent.type
        ]
    except KeyError:
        raise ConfigError(
            f"agent type '{agent.type}' has no built-in endpoint; it is either a "
            "CLI adapter or an unknown type"
        ) from None

    # A legacy ``openai_api`` pointed at a ``base_url`` keeps OPENAI_API_KEY
    # only when the entry named it — matching today's rule that an arbitrary
    # host never receives an ambient cloud key.
    key_env = agent.api_key_env or (
        default_key_env if agent.base_url is None else None
    )
    return EndpointConfig(
        id=endpoint_id,
        transport=transport,
        base_url=agent.base_url,
        api_key_env=key_env,
        location=location,
        display_name=_BUILTIN_DISPLAY.get(endpoint_id, endpoint_id),
    )
