"""Model registry access for the CLI — a thin adapter over ``mak.models``.

The catalog itself lives in the kernel (``mak/models/``): it is seeded from a
packaged list, refreshed from each provider's list-models API on the 1st and
15th (or on demand via ``/refresh-models``), and cached in
``~/.config/mak/models.json``.

``ModelInfo`` is an alias of ``mak.models.ModelEntry`` — one dataclass, not two —
so ``.api_key_env`` / ``.adapter_type`` keep working for existing call sites.

There is deliberately **no module-level ``ALL_MODELS`` list**: a list captured at
import time cannot reflect a refresh. Call ``all_models()`` instead.
"""
from __future__ import annotations

from mak.models import (
    KEY_ENV_TO_PROVIDER,
    PROVIDER_DISPLAY,
    PROVIDER_ORDER,
    ModelEntry,
    ModelRegistry,
)

# Backward-compatible alias: the CLI has always called this shape "ModelInfo".
ModelInfo = ModelEntry

__all__ = [
    "KEY_ENV_TO_PROVIDER",
    "PROVIDER_DISPLAY",
    "PROVIDER_ORDER",
    "ModelInfo",
    "all_models",
    "models_for_provider",
    "providers_with_keys",
    "recommended_planner_for_provider",
    "registry",
]

_REGISTRY = ModelRegistry()


def registry() -> ModelRegistry:
    """Return the process-wide model registry."""
    return _REGISTRY


def all_models() -> tuple[ModelInfo, ...]:
    """Return the current model catalog snapshot."""
    return _REGISTRY.all_models()


def models_for_provider(provider: str) -> list[ModelInfo]:
    """Return the catalog entries for one provider."""
    return list(_REGISTRY.for_provider(provider))


def providers_with_keys(api_keys: dict[str, str]) -> list[str]:
    """Return providers that have a non-empty API key configured."""
    return [KEY_ENV_TO_PROVIDER[k] for k, v in api_keys.items()
            if v.strip() and k in KEY_ENV_TO_PROVIDER]


def recommended_planner_for_provider(provider: str) -> str:
    """Return the planner model MAK auto-selects for ``provider``."""
    return _REGISTRY.recommended_planner(provider) or "claude-opus-5"
