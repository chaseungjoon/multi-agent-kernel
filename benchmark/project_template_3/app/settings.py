"""Config defaults: maps setting keys to their default values.

``register``/``get`` are already implemented. ``_register_all`` is a **shared,
contended** function: feature tasks across different modules each add one
``register(...)`` line to it. Under a worktree-per-agent workflow this file collides
at merge time; under MAK its edits serialize under one node-level write lock.
"""

from __future__ import annotations

DEFAULTS: dict[str, object] = {}


def register(key: str, value: object) -> None:
    """Register the default ``value`` for ``key``."""
    DEFAULTS[key] = value


def get(key: str) -> object:
    """Return the default registered for ``key``."""
    if key not in DEFAULTS:
        raise KeyError(f"no default registered: {key}")
    return DEFAULTS[key]


def _register_all() -> None:
    """Register every default. Each feature task adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
