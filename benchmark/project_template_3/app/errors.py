"""Error-code catalog: maps stable error codes to user-facing messages.

``register``/``message_for`` are already implemented. ``_register_all`` is a **shared,
contended** function: feature tasks across different modules each add one
``register(...)`` line to it. Under a worktree-per-agent workflow this file collides
at merge time; under MAK its edits serialize under one node-level write lock.
"""

from __future__ import annotations

ERRORS: dict[str, str] = {}


def register(code: str, message: str) -> None:
    """Register the user-facing ``message`` for ``code``."""
    ERRORS[code] = message


def message_for(code: str) -> str:
    """Return the message registered for ``code``."""
    if code not in ERRORS:
        raise KeyError(f"no error registered: {code}")
    return ERRORS[code]


def _register_all() -> None:
    """Register every error code. Each feature task adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
