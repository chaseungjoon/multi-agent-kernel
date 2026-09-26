"""Duplicate keys in a registrar function.

Two tasks each add ``register("/users", …)`` to a shared table. Node locks
serialize them correctly and both lines land — and the second registration
silently replaces the first at runtime. Textual merge has exactly the same blind
spot, so this is a place a kernel can do strictly better than git.

Scope and precision: only *registrar-shaped* functions are judged (see
:mod:`mak.node_store.registrar` — a flat list of calls to one callee, every
entry keyed by a string literal). A function that merely calls ``print("x")``
twice is not a table, and its repeated literal is not a key. And only
duplicates the edit **introduces** are reported: a table that already
registered a key twice before this edit is pre-existing debt, not this task's
conflict.
"""

from __future__ import annotations

from collections.abc import Mapping

from mak.node_store.registrar import duplicate_keys


def check_registry_keys(
    edits: Mapping[str, str], previous: Mapping[str, str] | None = None
) -> list[str]:
    """Report keys each edited registrar registers twice that it did not before.

    ``edits`` maps a node id to the source about to be committed; ``previous``
    maps it to the committed source it replaces (absent for a new node).
    """
    previous = previous or {}
    reasons: list[str] = []
    for node_id, source in edits.items():
        before = set(duplicate_keys(previous.get(node_id, "")))
        for key in duplicate_keys(source):
            if key in before:
                continue
            reasons.append(
                f"registry key collision: '{node_id}' registers key {key!r} more "
                "than once — the later registration silently replaces the earlier "
                "one at runtime. Use a distinct key, or extend the existing entry."
            )
    return reasons
