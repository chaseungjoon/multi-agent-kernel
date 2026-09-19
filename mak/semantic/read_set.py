"""The task read set — what a bundle carried, and at which version.

Before this, only the planner's ``context_nodes`` were read-locked, and nothing
at all was version-tracked. Everything ``Session._enrich_bundle`` added on its
own — same-file siblings, cross-file callers, dependency outputs — could be
rewritten by another task while the agent was working from the old copy, and
the commit that followed had no way to know. That is the whole of shape 1
(stale read / write skew).

A read set closes it the way optimistic concurrency control does in a
database: record ``(node, version, digest)`` for **every** node shipped, from
every layer, and at commit compare against what is committed now. Equal
digests everywhere means the task saw a consistent snapshot. Any difference is
a *stale read*, handed to :mod:`mak.semantic.stale` for a verdict.

Coverage is structural, not per-layer: marks are derived from the bundle's
context keys after enrichment, so a layer added later is covered without
anyone remembering to record it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from mak.core.types import NodeFragment, NodeId
from mak.node_store.store import source_digest

# Context-key prefixes that carry a node's committed source, and whether the
# agent saw only the node's interface (a ``read_api`` digest) rather than its code.
_SOURCE_PREFIXES = {"write_source": False, "read_source": False, "read_api": True}


@dataclass(frozen=True, slots=True)
class ReadMark:
    """One node as a bundle saw it.

    ``digest``/``version`` are ``None`` when the node did not exist at dispatch
    — a planner context node another task had yet to create. Reading "nothing"
    is still a read: if the node exists by commit time, the task was blind to it.
    ``source`` is the exact text shipped (kept in memory for the retry diff,
    never persisted). ``api_only`` marks a node the agent saw only as an API
    digest, so only an interface change to it can matter.
    """

    node_id: NodeId
    version: int | None
    digest: str | None
    layer: str
    api_only: bool = False
    source: str | None = None


ReadSet = dict[NodeId, ReadMark]


def mark_of(fragment: NodeFragment | None, node_id: NodeId, layer: str,
            *, api_only: bool = False) -> ReadMark:
    """Build the mark for ``fragment`` (``None`` = the node did not exist)."""
    if fragment is None:
        return ReadMark(node_id, None, None, layer, api_only)
    return ReadMark(
        node_id,
        fragment.version,
        source_digest(fragment.source),
        layer,
        api_only,
        fragment.source,
    )


def build_read_set(
    context: Mapping[str, str],
    layers: Mapping[str, list[str]],
    absent: Iterable[NodeId],
    fetch: Callable[[NodeId], NodeFragment | None],
    expand: Callable[[NodeId], list[NodeId]],
) -> ReadSet:
    """Derive a read set from an enriched bundle's context.

    ``layers`` maps a layer name to the context keys it added (the attribution
    ``TASK_DISPATCHED`` already logs), so each mark records which layer shipped
    it. ``fetch`` returns a node's committed fragment; ``expand`` lists the
    fragments of a whole-file id that has no node of its own (a dependency
    output assembled from fragments), each of which is marked individually.
    ``absent`` are planner context ids that had no committed node at dispatch.
    A node shipped by several keys keeps its first mark, and a full-source read
    outranks an ``api_only`` one.
    """
    layer_of = {key: name for name, keys in layers.items() for key in keys}
    marks: ReadSet = {}
    for key in context:
        prefix, _, raw_id = key.partition(":")
        if prefix not in _SOURCE_PREFIXES or not raw_id:
            continue
        api_only = _SOURCE_PREFIXES[prefix]
        layer = layer_of.get(key, prefix)
        node_id = NodeId(raw_id)
        fragment = fetch(node_id)
        ids = [node_id] if fragment is not None else expand(node_id)
        for member in ids:
            member_fragment = fragment if member == node_id else fetch(member)
            if member_fragment is None:
                continue
            _keep(marks, mark_of(member_fragment, member, layer, api_only=api_only))
    for node_id in absent:
        marks.setdefault(node_id, mark_of(None, node_id, "planner_context"))
    return marks


def _keep(marks: ReadSet, mark: ReadMark) -> None:
    """Record ``mark`` unless a stronger one for the node is already there."""
    held = marks.get(mark.node_id)
    if held is None or (held.api_only and not mark.api_only):
        marks[mark.node_id] = mark


def read_set_to_json(read_set: ReadSet) -> dict[str, dict[str, object]]:
    """Encode a read set for the task graph (sources are not persisted)."""
    return {
        str(node_id): {
            "version": mark.version,
            "digest": mark.digest,
            "layer": mark.layer,
            "api_only": mark.api_only,
        }
        for node_id, mark in read_set.items()
    }


def read_set_from_json(raw: object) -> ReadSet:
    """Decode a persisted read set; anything malformed is skipped, not raised.

    A read set only ever *adds* validation. Losing one to a damaged file costs
    the check for that attempt, which a recovered task re-dispatches anyway, so
    it is never a reason to fail recovery.
    """
    if not isinstance(raw, dict):
        return {}
    marks: ReadSet = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        version = value.get("version")
        digest = value.get("digest")
        marks[NodeId(str(key))] = ReadMark(
            node_id=NodeId(str(key)),
            version=version if isinstance(version, int) else None,
            digest=digest if isinstance(digest, str) else None,
            layer=str(value.get("layer", "")),
            api_only=bool(value.get("api_only", False)),
        )
    return marks
