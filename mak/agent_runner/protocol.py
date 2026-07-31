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

from mak.core.exceptions import AgentProtocolError
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

# The no-op half of the contract. MAK used to accept *any* successful result with
# no fragments as "the agent audited this and found nothing to change" — which is
# also exactly what a reply cut off at the output-token limit looks like, so
# truncated work was reported as a completed task. A no-op now needs the agent to
# say so, which a truncated reply can never do.
NO_CHANGE_CONTRACT = (
    "If — and only if — you inspected every target and no change is needed, set "
    "'no_changes_required' to true and return no fragments. An empty result "
    "without that flag is treated as a failed attempt, never as 'nothing to do'."
)

# The retry half. Populated by the session on a re-dispatch; absent on attempt 1.
RETRY_NOTE_CONTRACT = (
    "If the bundle carries a 'retry_note', your previous attempt at this task "
    "produced nothing usable for the reason it gives — follow its instruction "
    "instead of repeating that attempt."
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
        retry_note=data.get("retry_note"),
    )


def encode_task_result(result: TaskResult) -> str:
    """Serialize a TaskResult to a newline-delimited JSON string."""
    data = asdict(result)
    data["protocol_version"] = PROTOCOL_VERSION
    return json.dumps(data) + "\n"


def _type_name(value: object) -> str:
    """Return the JSON-ish name of ``value``'s type, for error messages."""
    return {
        dict: "object",
        list: "array",
        str: "string",
        bool: "boolean",
        int: "number",
        float: "number",
        type(None): "null",
    }.get(type(value), type(value).__name__)


def _require_mapping(data: object, field_name: str) -> dict[str, Any]:
    """Return ``data`` as a mapping, or raise naming the field and what arrived."""
    if not isinstance(data, dict):
        raise AgentProtocolError(
            f"'{field_name}' must be an object, got {_type_name(data)}"
        )
    return data


def _as_fragment_list(raw: object) -> list[dict[str, Any]]:
    """Normalize ``modified_fragments`` to a list of objects.

    The documented shape is an array of ``{node_id, new_source}``. A model that
    returns a **single object** instead is a common schema slip, and iterating it
    yields its *keys* — strings — so indexing them raised
    ``TypeError: string indices must be integers``, which the runner then
    reported as "api call failed" and the retry could do nothing with. One object
    is obviously one fragment, so coerce it; anything else is named and rejected.
    """
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if not isinstance(raw, list):
        raise AgentProtocolError(
            "'modified_fragments' must be an array of {node_id, new_source} "
            f"objects, got {_type_name(raw)}"
        )
    fragments: list[dict[str, Any]] = []
    for index, fragment in enumerate(raw):
        if not isinstance(fragment, dict):
            raise AgentProtocolError(
                f"'modified_fragments[{index}]' must be an object with 'node_id' "
                f"and 'new_source', got {_type_name(fragment)}"
            )
        fragments.append(fragment)
    return fragments


def _fragment_node_id(fragment: dict[str, Any], index: int) -> NodeId:
    node_id = fragment.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        raise AgentProtocolError(
            f"'modified_fragments[{index}].node_id' must be a non-empty string, "
            f"got {_type_name(node_id)}"
        )
    return NodeId(node_id)


def _fragment_source(fragment: dict[str, Any], index: int) -> str | None:
    """Return a fragment's ``new_source``, or None when the agent sent only an id."""
    source = fragment.get("new_source")
    if source is None:
        return None
    if not isinstance(source, str):
        raise AgentProtocolError(
            f"'modified_fragments[{index}].new_source' must be a string holding "
            f"the node's full source, got {_type_name(source)}"
        )
    return source


def _decode_usage(raw: object) -> dict[str, int]:
    """Return the provider's token counts, dropping anything not an integer."""
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): int(value)
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def decode_task_result(raw: str) -> TaskResult:
    """Deserialize a JSON string into a TaskResult.

    Accepts three shapes for the agent's rewritten source, in increasing
    specificity, and merges them into ``new_sources`` + ``modified_nodes``:

    - ``modified_nodes``: ids only (legacy / source already staged out-of-band);
    - ``modified_fragments``: an array of ``{node_id, new_source}`` (what the API
      adapters elicit from the model);
    - ``new_sources``: an explicit ``{node_id: source}`` mapping (the canonical
      field, e.g. from a re-encoded ``TaskResult``).

    Every malformed shape — including a payload cut off before its required
    fields arrived — raises ``AgentProtocolError`` naming the field and the type
    received, so the runner can report a decode failure as such and a retry has
    something to act on. It never raises a bare ``TypeError``/``KeyError``.
    """
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise AgentProtocolError(f"agent result was not valid JSON: {exc}") from exc
    data = _require_mapping(parsed, "result")
    _check_version(data)

    modified: list[NodeId] = []
    new_sources: dict[NodeId, str] = {}

    def _record(node_id: NodeId, source: str | None) -> None:
        if node_id not in modified:
            modified.append(node_id)
        if source is not None:
            new_sources[node_id] = source

    raw_modified = data.get("modified_nodes") or []
    if not isinstance(raw_modified, list):
        raise AgentProtocolError(
            f"'modified_nodes' must be an array of node ids, got "
            f"{_type_name(raw_modified)}"
        )
    for node_id in raw_modified:
        _record(NodeId(str(node_id)), None)

    for index, fragment in enumerate(_as_fragment_list(data.get("modified_fragments"))):
        _record(_fragment_node_id(fragment, index), _fragment_source(fragment, index))

    raw_sources = data.get("new_sources") or {}
    for node_id_str, source in _require_mapping(raw_sources, "new_sources").items():
        if not isinstance(source, str):
            raise AgentProtocolError(
                f"'new_sources[{node_id_str}]' must be a string holding the "
                f"node's full source, got {_type_name(source)}"
            )
        _record(NodeId(str(node_id_str)), source)

    for required in ("task_id", "success"):
        if required not in data:
            raise AgentProtocolError(
                f"agent result is missing the required '{required}' field "
                f"(present: {', '.join(sorted(data)) or 'nothing'})"
            )

    return TaskResult(
        task_id=str(data["task_id"]),
        success=bool(data["success"]),
        modified_nodes=modified,
        new_sources=new_sources,
        error=data.get("error"),
        no_changes_required=bool(data.get("no_changes_required", False)),
        stop_reason=data.get("stop_reason"),
        usage=_decode_usage(data.get("usage")),
        retryable=bool(data.get("retryable", True)),
    )
