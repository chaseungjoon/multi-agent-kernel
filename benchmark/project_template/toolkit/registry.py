"""Central operation registry and dispatcher.

``register``/``run`` are already implemented. ``_register_all`` is the **shared,
contended** function: every operation must add one ``register(...)`` line to it.
Because all the work funnels through this single function, a worktree-per-agent
workflow collides here at merge time, while MAK serializes the edits under one
node-level write lock so none are lost.
"""

from __future__ import annotations

from collections.abc import Callable

from toolkit import numbers, sequences, strings

OPERATIONS: dict[str, Callable[..., object]] = {}


def register(name: str, fn: Callable[..., object]) -> None:
    """Register operation ``fn`` under ``name``."""
    OPERATIONS[name] = fn


def run(name: str, *args: object) -> object:
    """Call the operation registered under ``name`` with ``args``."""
    if name not in OPERATIONS:
        raise KeyError(f"no operation registered: {name}")
    return OPERATIONS[name](*args)


def _register_all() -> None:
    """Register every operation. Each operation adds one ``register(...)`` line."""
    raise NotImplementedError


_register_all()
