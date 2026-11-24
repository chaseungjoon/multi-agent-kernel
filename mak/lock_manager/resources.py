"""Derived lock resources: a node's interface, and a registry entry.

The lock table is keyed by plain strings, so a finer-grained resource does not
need a new table — only a naming rule every caller shares. The derived kinds:

- ``<node_id>#api`` — the node's *interface* (whatever
  :func:`mak.node_store.api_digest.api_fingerprint` renders). A task calling X
  takes READ on ``X#api``; a task changing X's signature takes WRITE on it. The
  bare ``<node_id>`` stays the lock on the node's *body*, so a body-only edit no
  longer serializes with X's callers.
- ``<node_id>#key=<literal>`` — one entry of a keyed registrar node
  (``register("<literal>", …)``). Tasks appending different keys to the same
  table run in parallel; the same key serializes and is a detected collision.
- ``<header_id>#imports`` and ``<header_id>#import=<name>`` — the import block
  of a module header, and one name it binds. A task merging imports co-holds
  INTENT_WRITE on the block and WRITE-locks each name it adds, so tasks adding
  different imports to one header never serialize on it.

Ingestion already uses ``#<n>`` to disambiguate repeated names
(``Class.method#2``), so every derived suffix starts with a non-digit and the
base id is recovered by splitting on the *first* derived marker, never on the
last ``#``.
"""

from __future__ import annotations

from mak.core.types import NodeId

_API_SUFFIX = "#api"
_KEY_MARKER = "#key="
_IMPORTS_SUFFIX = "#imports"
_IMPORT_MARKER = "#import="


def api_resource(node_id: NodeId | str) -> NodeId:
    """Return the lock resource guarding ``node_id``'s interface."""
    return NodeId(f"{node_id}{_API_SUFFIX}")


def key_resource(node_id: NodeId | str, key: str) -> NodeId:
    """Return the lock resource guarding one key of a registrar node."""
    return NodeId(f"{node_id}{_KEY_MARKER}{key}")


def imports_resource(header_id: NodeId | str) -> NodeId:
    """Return the lock resource guarding a module header's import block."""
    return NodeId(f"{header_id}{_IMPORTS_SUFFIX}")


def import_name_resource(header_id: NodeId | str, name: str) -> NodeId:
    """Return the lock resource guarding one name a header's imports bind."""
    return NodeId(f"{header_id}{_IMPORT_MARKER}{name}")


def is_import_resource(resource: str) -> bool:
    """Whether ``resource`` is an import-block or imported-name lock."""
    return resource.endswith(_IMPORTS_SUFFIX) or _IMPORT_MARKER in resource


def is_api_resource(resource: str) -> bool:
    """Whether ``resource`` is an ``#api`` interface lock."""
    return resource.endswith(_API_SUFFIX) and _KEY_MARKER not in resource


def is_key_resource(resource: str) -> bool:
    """Whether ``resource`` is a registrar ``#key=`` lock."""
    return _KEY_MARKER in resource


def is_derived(resource: str) -> bool:
    """Whether ``resource`` names something finer than a whole node."""
    return (
        is_api_resource(resource)
        or is_key_resource(resource)
        or is_import_resource(resource)
    )


def base_node(resource: str) -> NodeId:
    """Return the node a (possibly derived) lock resource belongs to."""
    if is_key_resource(resource):
        return NodeId(resource.split(_KEY_MARKER, 1)[0])
    if _IMPORT_MARKER in resource:
        return NodeId(resource.split(_IMPORT_MARKER, 1)[0])
    if resource.endswith(_IMPORTS_SUFFIX):
        return NodeId(resource[: -len(_IMPORTS_SUFFIX)])
    if is_api_resource(resource):
        return NodeId(resource[: -len(_API_SUFFIX)])
    return NodeId(resource)
