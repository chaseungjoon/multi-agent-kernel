"""TaskBundle and TaskResult JSON serialization for the agent protocol.

The wire schema is exactly the ``TaskBundle`` / ``TaskResult`` dataclasses (the
single canonical schema). ``decode_task_bundle`` rebuilds nested ``LockEntry`` /
``ResourceRef`` objects rather than leaving raw dicts in a field typed
``list[LockEntry]``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from mak.core.types import (
    LockEntry,
    LockMode,
    NodeId,
    ResourceKind,
    ResourceRef,
    TaskBundle,
    TaskResult,
)

PROTOCOL_VERSION = "1.0"

# The node-granularity half of the contract, stated for the model in every
# adapter's system prompt. MAK grants write locks per node id, so an id it did
# not grant is an id it cannot safely apply; a returned id that merely *looks*
# related (``file.py::function::f`` under a whole-file grant of ``file.py``) used
# to be discarded without a word. The session now folds that particular case
# rather than losing the work, but the cheapest fix is for it not to happen:
# say plainly that ids are copied, never composed.
NODE_ID_CONTRACT = (
    "Use node ids exactly as they appear in the bundle's 'target_nodes' — copy "
    "them verbatim, never invent narrower, broader, or renamed ids. A target "
    "that is a bare file path (no '::') is a whole file: return its complete "
    "source under that exact id, not one entry per function."
)


def map_returned_sources(
    grant: list[NodeId], new_sources: dict[NodeId, str]
) -> tuple[dict[NodeId, str], list[tuple[NodeId, str]]]:
    """Map returned node ids onto nodes the task may write; report the rest.

    This is the enforcement half of :data:`NODE_ID_CONTRACT`, shared by every
    path that receives agent output (the session's API transport and the CLI
    bridge) so one rule governs all of them. Returns ``(accepted, dropped)``,
    where every returned id lands in exactly one:

    - it *is* a granted id — accepted as itself;
    - it names a symbol inside a file granted as a **whole-file** (bare-path)
      node — folded into that whole-file node. An agent handed the greenfield
      grant ``editor/motions.py`` and returning
      ``editor/motions.py::function::move_word`` has done the work; discarding it
      failed the task and its whole dependent subtree over a naming
      disagreement. Several fragments for one grant are concatenated in the order
      the agent returned them, and an explicit whole-file source always wins over
      folded fragments;
    - anything else — dropped, with the reason, for the caller to report. A
      source for a file the task was never granted must not be applied: those
      nodes belong to other tasks and are not write-locked here.
    """
    in_scope = set(grant)
    whole_file_grants = {str(n) for n in grant if "::" not in str(n)}
    accepted: dict[NodeId, str] = {}
    folded: dict[NodeId, list[str]] = {}
    dropped: list[tuple[NodeId, str]] = []
    for node_id, source in new_sources.items():
        if node_id in in_scope:
            accepted[node_id] = source
            continue
        file_path = str(node_id).split("::", 1)[0]
        if file_path in whole_file_grants:
            folded.setdefault(NodeId(file_path), []).append(source)
            continue
        dropped.append(
            (node_id, "returned node id is outside the task's granted nodes")
        )
    for whole_file_id, parts in folded.items():
        if whole_file_id in accepted:
            continue  # the agent also sent the whole file; that is authoritative
        accepted[whole_file_id] = "\n\n".join(p.strip("\n") for p in parts) + "\n"
    return accepted, dropped


def _check_version(data: dict[str, Any]) -> None:
    version = data.pop("protocol_version", None)
    if version is not None and version != PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported protocol version: {version} (expected {PROTOCOL_VERSION})"
        )


def _decode_lock_entry(raw: dict[str, Any]) -> LockEntry:
    resource = raw["resource"]
    return LockEntry(
        resource=ResourceRef(
            kind=ResourceKind(resource["kind"]),
            path=resource["path"],
            symbol=resource.get("symbol"),
        ),
        mode=LockMode(raw["mode"]),
        holder=raw["holder"],
        acquired_at=raw["acquired_at"],
    )


def encode_task_bundle(bundle: TaskBundle) -> str:
    """Serialize a TaskBundle to a newline-delimited JSON string."""
    data = asdict(bundle)
    data["protocol_version"] = PROTOCOL_VERSION
    return json.dumps(data) + "\n"


def decode_task_bundle(raw: str) -> TaskBundle:
    """Deserialize a JSON string into a TaskBundle."""
    data = json.loads(raw.strip())
    _check_version(data)
    return TaskBundle(
        task_id=data["task_id"],
        description=data["description"],
        target_nodes=[NodeId(n) for n in data.get("target_nodes", [])],
        locks=[_decode_lock_entry(e) for e in data.get("locks", [])],
        context=data.get("context", {}),
    )


def encode_task_result(result: TaskResult) -> str:
    """Serialize a TaskResult to a newline-delimited JSON string."""
    data = asdict(result)
    data["protocol_version"] = PROTOCOL_VERSION
    return json.dumps(data) + "\n"


def decode_task_result(raw: str) -> TaskResult:
    """Deserialize a JSON string into a TaskResult.

    Accepts three shapes for the agent's rewritten source, in increasing
    specificity, and merges them into ``new_sources`` + ``modified_nodes``:

    - ``modified_nodes``: ids only (legacy / source already staged out-of-band);
    - ``modified_fragments``: an array of ``{node_id, new_source}`` (what the API
      adapters elicit from the model);
    - ``new_sources``: an explicit ``{node_id: source}`` mapping (the canonical
      field, e.g. from a re-encoded ``TaskResult``).
    """
    data = json.loads(raw.strip())
    _check_version(data)

    modified: list[NodeId] = [NodeId(n) for n in data.get("modified_nodes", [])]
    new_sources: dict[NodeId, str] = {}

    def _record(node_id: NodeId, source: str | None) -> None:
        if node_id not in modified:
            modified.append(node_id)
        if source is not None:
            new_sources[node_id] = source

    for fragment in data.get("modified_fragments") or []:
        node_id = NodeId(str(fragment["node_id"]))
        raw_source = fragment.get("new_source")
        _record(node_id, None if raw_source is None else str(raw_source))
    for node_id_str, source in (data.get("new_sources") or {}).items():
        _record(NodeId(str(node_id_str)), str(source))

    return TaskResult(
        task_id=data["task_id"],
        success=data["success"],
        modified_nodes=modified,
        new_sources=new_sources,
        error=data.get("error"),
    )
