"""The planner's credential: the one resolver both front ends use."""

from __future__ import annotations

import os
from collections.abc import Mapping

from mak.bootstrap import DEFAULT_KEY_ENV
from mak.config import MakConfig
from mak.endpoints.resolution import (
    ResolvedEndpoint,
    require_endpoint,
    resolve_endpoints,
)
from mak.endpoints.store import load_user_endpoints, merge_endpoints

# Planner backend naming a hosted provider -> that provider's adapter type,
# whose conventional key variable ``DEFAULT_KEY_ENV`` holds.
_HOSTED_BACKENDS: dict[str, str] = {
    "anthropic": "anthropic_api",
    "openai": "openai_api",
    "gemini": "gemini_api",
}


def planner_endpoint(
    config: MakConfig, *, env: Mapping[str, str] | None = None
) -> ResolvedEndpoint | None:
    """Return the planner's resolved endpoint, or None when it names none.

    Resolved against the merged endpoint set, so a planner may route through an
    endpoint the user saved interactively and never wrote into this file.
    """
    if not config.planner.endpoint:
        return None
    user_endpoints, _diagnostic = load_user_endpoints()
    merged = merge_endpoints(config.endpoints, user_endpoints)
    resolved = resolve_endpoints(merged, env=env)
    return require_endpoint(resolved, config.planner.endpoint, where="planner")


def resolve_planner_key(
    config: MakConfig, env: Mapping[str, str] | None = None
) -> str | None:
    """Resolve the planner's API key: endpoint, explicit env var, then inference.

    ``env`` defaults to the process environment; the interactive app passes the
    environment overlaid with its session keys instead of exporting them.

    An endpoint is authoritative when one is named: it stated which variable
    holds its credential, so there is nothing to infer, and inferring anyway
    would let one service's key reach another's host.

    ``planner.api_key_env`` wins next — it is the only way to name the token
    for a protected gateway (``vllm --api-key``), whose model id tells us
    nothing. Falling through to ``None`` is deliberate rather than an oversight:
    a local planner has no key, and ``None`` is what lets the adapter apply its
    placeholder rule instead of forwarding a real cloud key to a local host.
    """
    source = os.environ if env is None else env
    endpoint = planner_endpoint(config, env=source)
    if endpoint is not None:
        return endpoint.api_key
    planner = config.planner
    if planner.api_key_env:
        return source.get(planner.api_key_env)
    if planner.base_url is None and planner.backend in _HOSTED_BACKENDS:
        # A named hosted provider decides the key; the model id may be one
        # several providers serve, so it is not guessed from.
        return source.get(DEFAULT_KEY_ENV[_HOSTED_BACKENDS[planner.backend]])
    if planner.backend is not None or planner.base_url is not None:
        return None
    return _inferred_key(config, source)


def _inferred_key(config: MakConfig, env: Mapping[str, str]) -> str | None:
    """Guess the key from a bare model id — only for a config naming no route."""
    model = config.planner.model.lower()
    if model.startswith("claude"):
        backend = "anthropic_api"
    elif model.startswith("gemini"):
        backend = "gemini_api"
    elif model.startswith(("gpt", "o1", "o3", "o4")):
        backend = "openai_api"
    else:
        return None
    for agent in config.agents:
        if agent.type == backend and agent.api_key_env:
            return env.get(agent.api_key_env)
    # The planner's provider may not be in the roster (e.g. an OpenAI-only run with
    # the default Claude planner) — fall back to that provider's conventional env var.
    fallback_env = DEFAULT_KEY_ENV.get(backend)
    return env.get(fallback_env) if fallback_env else None
