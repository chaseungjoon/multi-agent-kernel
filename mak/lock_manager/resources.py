"""Derived lock resources: a node's interface, and a registry entry.

The lock table is keyed by plain strings, so a finer-grained resource does not
need a new table — only a naming rule every caller shares. Two derived kinds:

- ``<node_id>#api`` — the node's *interface* (whatever
  :func:`mak.node_store.api_digest.api_fingerprint` renders). A task calling X
  takes READ on ``X#api``; a task changing X's signature takes WRITE on it. The
  bare ``<node_id>`` stays the lock on the node's *body*, so a body-only edit no
  longer serializes with X's callers.
- ``<node_id>#key=<literal>`` — one entry of a keyed registrar node
  (``register("<literal>", …)``). Tasks appending different keys to the same
  table run in parallel; the same key serializes and is a detected collision.

Ingestion already uses ``#<n>`` to disambiguate repeated names
(``Class.method#2``), so every derived suffix starts with a non-digit and the
base id is recovered by splitting on the *first* derived marker, never on the
last ``#``.
"""

from __future__ import annotations

from mak.core.types import NodeId

_API_SUFFIX = "#api"
_KEY_MARKER = "#key="


def api_resource(node_id: NodeId | str) -> NodeId:
    """Return the lock resource guarding ``node_id``'s interface."""
    return NodeId(f"{node_id}{_API_SUFFIX}")


def key_resource(node_id: NodeId | str, key: str) -> NodeId:
    """Return the lock resource guarding one key of a registrar node."""
    return NodeId(f"{node_id}{_KEY_MARKER}{key}")


def is_api_resource(resource: str) -> bool:
    """Whether ``resource`` is an ``#api`` interface lock."""
    return resource.endswith(_API_SUFFIX) and _KEY_MARKER not in resource


def is_key_resource(resource: str) -> bool:
    """Whether ``resource`` is a registrar ``#key=`` lock."""
    return _KEY_MARKER in resource


def is_derived(resource: str) -> bool:
    """Whether ``resource`` names something finer than a whole node."""
    return is_api_resource(resource) or is_key_resource(resource)


def base_node(resource: str) -> NodeId:
    """Return the node a (possibly derived) lock resource belongs to."""
    if is_key_resource(resource):
        return NodeId(resource.split(_KEY_MARKER, 1)[0])
    if is_api_resource(resource):
        return NodeId(resource[: -len(_API_SUFFIX)])
    return NodeId(resource)
