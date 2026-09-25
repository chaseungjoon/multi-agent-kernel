"""Model registry access for the CLI — a thin adapter over ``mak.models``.

The catalog itself lives in the kernel (``mak/models/``): it is seeded from a
packaged list, refreshed from each provider's list-models API on the 1st and
15th (or on demand via ``/refresh-models``), and cached in
``~/.config/mak/models.json``.

There is no process-wide registry: the app's ``CliState`` builds one lazily on
first use (``state.models()``) and every helper here takes it explicitly, so
importing this module reads nothing from disk and a test can hand in its own.

``ModelInfo`` is an alias of ``mak.models.ModelEntry`` — one dataclass, not two.
Its ``.api_key_env`` / ``.adapter_type`` answer only for the three built-in
providers and are ``None`` for an endpoint's entry, whose endpoint owns both.

There is deliberately **no module-level ``ALL_MODELS`` list**: a list captured at
import time cannot reflect a refresh. Call ``all_models(registry)`` instead.
"""
from __future__ import annotations

from collections.abc import Mapping

from mak.application.route import PlannerRoute
from mak.models import (
    KEY_ENV_TO_PROVIDER,
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    ModelEntry,
    ModelRegistry,
)

# Backward-compatible alias: the CLI has always called this shape "ModelInfo".
ModelInfo = ModelEntry

# The planner a fresh session starts with before any key is known.
DEFAULT_PLANNER_PROVIDER = "anthropic"
DEFAULT_PLANNER_MODEL = "claude-opus-5"

__all__ = [
    "DEFAULT_PLANNER_MODEL",
    "DEFAULT_PLANNER_PROVIDER",
    "KEY_ENV_TO_PROVIDER",
    "PROVIDER_DISPLAY",
    "PROVIDER_ORDER",
    "ModelInfo",
    "all_models",
    "default_planner_route",
    "models_for_provider",
    "providers_with_keys",
    "recommended_planner_for_provider",
]


def all_models(registry: ModelRegistry) -> tuple[ModelInfo, ...]:
    """Return the current model catalog snapshot."""
    return registry.all_models()


def models_for_provider(registry: ModelRegistry, provider: str) -> list[ModelInfo]:
    """Return the catalog entries for one provider."""
    return list(registry.for_provider(provider))


def providers_with_keys(api_keys: Mapping[str, str]) -> list[str]:
    """Return providers that have a non-empty API key configured."""
    return [KEY_ENV_TO_PROVIDER[k] for k, v in api_keys.items()
            if v.strip() and k in KEY_ENV_TO_PROVIDER]


def recommended_planner_for_provider(registry: ModelRegistry, provider: str) -> str:
    """Return the planner model MAK auto-selects for ``provider``."""
    return registry.recommended_planner(provider) or DEFAULT_PLANNER_MODEL


def default_planner_route(
    registry: ModelRegistry, api_keys: Mapping[str, str]
) -> PlannerRoute:
    """Return the hosted planner a session falls back to.

    The recommended planner of the first provider with a key, or MAK's default
    when there is none yet.
    """
    available = providers_with_keys(api_keys)
    if not available:
        return PlannerRoute.hosted(DEFAULT_PLANNER_PROVIDER, DEFAULT_PLANNER_MODEL)
    provider = available[0]
    return PlannerRoute.hosted(
        provider, recommended_planner_for_provider(registry, provider)
    )
