"""Resolve profile + endpoint + agent into immutable records the kernel uses.

Resolution happens **before** any adapter factory is registered, so the
composition root never has to ask "what did the profile say?" at dispatch time,
and nothing downstream carries a half-decided setting.

Precedence, top wins:

1. an explicit field on the endpoint (or on the agent, for generation limits);
2. the selected profile's default;
3. the transport's own default.

``None`` at any layer means *defer to the next one down*. An explicit ``none``
member is a decision and stops the walk — see ``mak.endpoints.types``.

Nothing here mutates its inputs: every function returns a new frozen record.
That is what lets the CLI hold a resolved view and the config hold the source of
truth without either drifting.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from mak.core.exceptions import ConfigError
from mak.endpoints.profiles import adapter_type_for, profile_for
from mak.endpoints.types import (
    EndpointConfig,
    HealthPolicy,
    Location,
    ModelDiscovery,
    ProviderRouting,
    StructuredOutput,
    TokenParameter,
    Transport,
)

# Sent as the API key whenever an endpoint names no credential variable. The
# SDK requires *some* key; this one is deliberately not a secret, and sending it
# is what stops the SDK reading a real ``OPENAI_API_KEY`` from the environment
# and POSTing it to whatever host the config names.
PLACEHOLDER_KEY = "local"

_T = TypeVar("_T")

# Transport-level defaults — the bottom of the precedence walk. A profile
# overrides these for a service whose behaviour is known; an endpoint field
# overrides both.
_TRANSPORT_DEFAULTS: dict[Transport, tuple[ModelDiscovery, HealthPolicy, StructuredOutput, TokenParameter]] = {  # noqa: E501
    Transport.OPENAI_CHAT: (
        ModelDiscovery.AUTO,
        HealthPolicy.MODELS,
        StructuredOutput.AUTO,
        TokenParameter.AUTO,
    ),
    Transport.ANTHROPIC: (
        ModelDiscovery.MODELS,
        HealthPolicy.NONE,
        StructuredOutput.NONE,
        TokenParameter.MAX_TOKENS,
    ),
    Transport.GEMINI: (
        ModelDiscovery.MODELS,
        HealthPolicy.NONE,
        StructuredOutput.NONE,
        TokenParameter.NONE,
    ),
    Transport.OLLAMA_NATIVE: (
        ModelDiscovery.MODELS,
        HealthPolicy.MODELS,
        StructuredOutput.JSON_SCHEMA,
        TokenParameter.MAX_TOKENS,
    ),
}


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    """One endpoint with every capability decided and no ``None`` left.

    Built once, at composition time. Adapters, health checks, model discovery
    and the CLI all read this rather than re-deriving defaults, which is how the
    four of them stay in agreement.

    ``api_key`` is resolved from the environment here and is the **only** place
    a secret enters the endpoint layer. It is never serialized, never logged and
    never written to the endpoint store.
    """

    id: str
    display_name: str
    transport: Transport
    base_url: str | None
    location: Location
    model_discovery: ModelDiscovery
    health_check: HealthPolicy
    structured_output: StructuredOutput
    token_parameter: TokenParameter
    # Which provider-routing extension, if any, this endpoint's request body
    # may carry. Decided here from the profile so the adapter never has to ask
    # "is this OpenRouter?" — a question a hostname cannot answer.
    provider_routing: ProviderRouting = ProviderRouting.NONE
    api_key_env: str | None = None
    api_key: str | None = None
    headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    profile: str | None = None
    # Set by the CLI when the user has accepted that a chat probe may be billed.
    chat_probe_allowed: bool = False

    @property
    def adapter_type(self) -> str:
        """Return the adapter class selector for this endpoint's transport."""
        return adapter_type_for(self.transport)

    @property
    def is_hosted(self) -> bool:
        """Whether traffic to this endpoint leaves the user's own network."""
        return self.location is Location.HOSTED

    @property
    def has_key(self) -> bool:
        """Whether a real credential (not the placeholder) was resolved."""
        return bool(self.api_key)

    def effective_key(self) -> str | None:
        """Return the key to hand the SDK: the resolved one, or the placeholder.

        Never ``None`` for an endpoint with a ``base_url``. Always sending
        *something* explicit is what prevents the SDK falling back to reading
        ``OPENAI_API_KEY`` itself and forwarding a real cloud key to a
        third-party host — the one security property of this transport.
        """
        if self.api_key:
            return self.api_key
        if self.base_url:
            return PLACEHOLDER_KEY
        return None

    def sanitized_base_url(self) -> str:
        """Return the base URL with any query string removed, for display."""
        if not self.base_url:
            return ""
        return self.base_url.split("?", 1)[0]

    def describe(self) -> str:
        """Return a one-line, secret-free description for status and logs."""
        where = self.location.value
        url = self.sanitized_base_url() or "(sdk default)"
        return f"{self.display_name} [{where}] {url}"


@dataclass(frozen=True, slots=True)
class ResolvedAgentConfig:
    """One schedulable agent: an id, a model, and the endpoint behind it.

    ``id`` is the routing key — registry, scheduler, pool caps, planner choice,
    logs and git metadata all use it. ``adapter_type`` only selects which class
    to construct, and two agents may legitimately share it. Separating the two
    is the whole of Wave 22's structural change.

    ``endpoint`` is ``None`` only for the CLI wrapper adapters (``claude_code``,
    ``codex``, ``copilot``), which drive a local binary and have no URL,
    credential or capability negotiation to resolve.
    """

    id: str
    adapter_type: str
    endpoint: ResolvedEndpoint | None = None
    model: str | None = None
    max_instances: int = 2
    timeout: int = 300
    max_tokens: int | None = None
    # A per-agent override of the endpoint's structured-output policy. Unset
    # (the common case) means "use whatever the endpoint resolved to"; it exists
    # because one small model behind an otherwise capable endpoint may need a
    # lower rung than its neighbours.
    structured_output: str | None = None
    repair_attempts: int | None = None
    num_ctx: int | None = None
    keep_alive: str | None = None
    temperature: float | None = None
    cmd: str | None = None

    @property
    def is_local(self) -> bool:
        """Whether this agent's work stays on the user's machine or network.

        Reads the endpoint's explicit ``location`` rather than guessing from a
        ``base_url`` — every hosted compatible service has one of those too.
        """
        if self.endpoint is None:
            return False
        return self.endpoint.location is not Location.HOSTED

    def label(self) -> str:
        """Return the planner-facing label: id, model and endpoint name.

        Deliberately excludes the URL, the headers and the credential variable:
        the planner prompt is sent to a model, and an internal hostname in it is
        an information leak with no planning value.
        """
        model = self.model or "(adapter default)"
        via = self.endpoint.display_name if self.endpoint else self.adapter_type
        return f"{self.id} — {model} via {via}"


def _pick(explicit: _T | None, from_profile: _T | None, default: _T) -> _T:
    """Walk the precedence chain, treating only ``None`` as "unset".

    An explicit ``none`` member is a value, not an absence, so it stops the
    walk here rather than falling through to the profile's default.
    """
    if explicit is not None:
        return explicit
    if from_profile is not None:
        return from_profile
    return default


def resolve_endpoint(
    endpoint: EndpointConfig,
    *,
    env: Mapping[str, str] | None = None,
    chat_probe_allowed: bool = False,
) -> ResolvedEndpoint:
    """Resolve one endpoint's capabilities and credential against ``env``.

    ``env`` defaults to the process environment. It is a parameter so tests —
    and the CLI, which holds keys the process has not exported yet — can resolve
    against an explicit mapping instead of mutating ``os.environ``.
    """
    source = os.environ if env is None else env
    profile = profile_for(endpoint.profile) if endpoint.profile else None
    discovery_d, health_d, structured_d, token_d = _TRANSPORT_DEFAULTS.get(
        endpoint.transport,
        (
            ModelDiscovery.AUTO,
            HealthPolicy.MODELS,
            StructuredOutput.AUTO,
            TokenParameter.AUTO,
        ),
    )

    api_key = (
        source.get(endpoint.api_key_env, "").strip() if endpoint.api_key_env else ""
    )

    return ResolvedEndpoint(
        id=endpoint.id,
        display_name=endpoint.display_name or endpoint.id,
        transport=endpoint.transport,
        base_url=endpoint.base_url,
        location=endpoint.location,
        model_discovery=_pick(
            endpoint.model_discovery,
            profile.model_discovery if profile else None,
            discovery_d,
        ),
        health_check=_pick(
            endpoint.health_check,
            profile.health_check if profile else None,
            health_d,
        ),
        structured_output=_pick(
            endpoint.structured_output,
            profile.structured_output if profile else None,
            structured_d,
        ),
        token_parameter=_pick(
            endpoint.token_parameter,
            profile.token_parameter if profile else None,
            token_d,
        ),
        provider_routing=_pick(
            endpoint.provider_routing,
            profile.provider_routing if profile else None,
            # No transport default: a routing extension is a property of one
            # named service, so an endpoint with no profile claiming it sends
            # standard fields only.
            ProviderRouting.NONE,
        ),
        api_key_env=endpoint.api_key_env,
        api_key=api_key or None,
        headers=_resolve_headers(endpoint, source),
        profile=endpoint.profile,
        chat_probe_allowed=chat_probe_allowed,
    )


def _resolve_headers(
    endpoint: EndpointConfig, env: Mapping[str, str]
) -> tuple[tuple[str, str], ...]:
    """Resolve configured headers to ``(name, value)`` pairs.

    A header whose ``value_env`` is unset is **dropped**, not sent empty: an
    empty ``X-Token:`` header is a request that looks authenticated and is not,
    and the resulting 401 is harder to read than the header simply being absent.
    """
    resolved: list[tuple[str, str]] = []
    for header in endpoint.headers:
        if header.value is not None:
            resolved.append((header.name, header.value))
            continue
        if header.value_env:
            value = env.get(header.value_env, "").strip()
            if value:
                resolved.append((header.name, value))
    return tuple(resolved)


def resolve_endpoints(
    endpoints: tuple[EndpointConfig, ...],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, ResolvedEndpoint]:
    """Resolve every endpoint, returning them keyed by id."""
    return {e.id: resolve_endpoint(e, env=env) for e in endpoints}


def require_endpoint(
    resolved: dict[str, ResolvedEndpoint], endpoint_id: str, *, where: str
) -> ResolvedEndpoint:
    """Return the named endpoint, or raise ``ConfigError`` listing what exists."""
    endpoint = resolved.get(endpoint_id)
    if endpoint is not None:
        return endpoint
    known = ", ".join(sorted(resolved)) or "none configured"
    raise ConfigError(
        f"{where} names endpoint '{endpoint_id}', which is not configured; "
        f"known endpoints: {known}. Add one with '/endpoint add' or an "
        "'endpoints:' entry in mak.yaml."
    )
