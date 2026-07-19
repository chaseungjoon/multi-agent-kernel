"""URL route table: maps route keys to feature handlers.

``register``/``dispatch`` are already implemented. ``_register_all`` is a **shared,
contended** function: feature tasks across different modules each add one
``register(...)`` line to it. Under a worktree-per-agent workflow this file collides
at merge time; under MAK its edits serialize under one node-level write lock.
"""

from __future__ import annotations

from collections.abc import Callable

from app import accounts, cart, catalog, orders, payments, reviews, search, shipping

ROUTES: dict[str, Callable[..., object]] = {}


def register(pattern: str, handler: Callable[..., object]) -> None:
    """Register ``handler`` for ``pattern`` (e.g. "GET /cart/total")."""
    ROUTES[pattern] = handler


def dispatch(pattern: str, *args: object) -> object:
    """Call the handler registered for ``pattern`` with ``args``."""
    if pattern not in ROUTES:
        raise KeyError(f"no route registered: {pattern}")
    return ROUTES[pattern](*args)


def _register_all() -> None:
    """Register every route. Each feature task adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
