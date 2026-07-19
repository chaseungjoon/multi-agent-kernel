"""Domain-event handler table: maps event names to feature handlers.

``register``/``emit`` are already implemented. ``_register_all`` is a **shared,
contended** function: feature tasks across different modules each add one
``register(...)`` line to it. Under a worktree-per-agent workflow this file collides
at merge time; under MAK its edits serialize under one node-level write lock.
"""

from __future__ import annotations

from collections.abc import Callable

from app import accounts, cart, catalog, orders, payments, reviews, search, shipping

HANDLERS: dict[str, Callable[..., object]] = {}


def register(event: str, handler: Callable[..., object]) -> None:
    """Register ``handler`` for ``event`` (e.g. "order.placed")."""
    HANDLERS[event] = handler


def emit(event: str, *args: object) -> object:
    """Invoke the handler registered for ``event`` with ``args``."""
    if event not in HANDLERS:
        raise KeyError(f"no handler registered: {event}")
    return HANDLERS[event](*args)


def _register_all() -> None:
    """Register every event handler. Each feature task adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
