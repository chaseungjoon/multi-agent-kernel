"""TaskBundle and TaskResult JSON serialization for the agent protocol.

The wire schema is exactly the ``TaskBundle`` / ``TaskResult`` dataclasses (the
single canonical schema). ``decode_task_bundle`` rebuilds nested ``LockEntry`` /
``ResourceRef`` objects rather than leaving raw dicts in a field typed
``list[LockEntry]``.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, NamedTuple

from mak.core.exceptions import AgentProtocolError
from mak.core.types import (
    LockEntry,
    LockMode,
    NodeId,
    NodeNeed,
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

# A target the store has no source for yet (a new file, or a new symbol in an
# existing one) arrives with no 'write_source' entry at all. Without saying what
# that means, a model reads the gap as missing input and refuses — "the context
# is empty, please provide the existing source" — and every retry repeats it.
NEW_TARGET_CONTRACT = (
    "A target with no 'write_source' entry does not exist yet: it is a new file "
    "or symbol for you to create. There is no existing source to ask for — write "
    "its complete source from scratch under that exact id."
)

# The header half. Every task that needed an import used to have to WRITE-lock
# the file's one ``module_header`` node, so tasks in one file serialized on it —
# or, when the planner left the header out, the import was dropped. Imports now
# travel on their own channel and the kernel merges them.
IMPORTS_CONTRACT = (
    "Never edit a file's module header just to add an import: list each import "
    "statement you need in 'imports' instead (one statement per entry, e.g. "
    "'from typing import Any'), and the kernel merges it into the header. When "
    "your targets span several files, prefix the entry with its file and '::' "
    "('pkg/mod.py::import os')."
)

# The rest of the write-set growth contract: a new helper beside a target, and
# a node the plan did not predict.
GROWTH_CONTRACT = (
    "You may add a new top-level function or class next to a target in the "
    "same file: return it under a new id ('<file>::function::<name>' or "
    "'<file>::class::<name>') and, optionally, set its fragment's "
    "'insert_after' to the id it should follow. If you need to read or change "
    "a node you were not given, list it in 'needs_nodes' as {node_id, mode "
    "('read' or 'write'), reason} instead of guessing at its source."
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

# The *in-adapter* repair turn. Distinct from RETRY_NOTE_CONTRACT, which is a
# standing instruction in the system prompt about a field the next bundle may
# carry: this is the follow-up message the adapter sends when a reply arrived but
# could not be decoded. Repairing in the adapter costs one short turn; the
# alternative — failing the attempt and letting the session re-dispatch — re-sends
# the entire bundle (write sources, sibling context, caller context: tens of KB).
# For a small local model a malformed first reply is the common case, not the
# rare one, so the cheap path has to be the default one.
REPAIR_INSTRUCTION = (
    "Your previous reply could not be decoded: {reason}. Reply again with only "
    "a single JSON object and nothing else — no prose, no explanation, no code "
    "fences. It must have exactly these keys: 'task_id' (string, echoing the "
    "bundle's task_id), 'success' (boolean), 'modified_fragments' (array of "
    "objects, each with 'node_id' and the node's FULL rewritten 'new_source'), "
    "'no_changes_required' (boolean), and 'error' (string when success is "
    "false, otherwise null). 'imports' (array of import statements) and "
    "'needs_nodes' (array of {{node_id, mode, reason}}) are optional. Do not "
    "change the work you did — only its shape."
)

# The retry half. Populated by the session on a re-dispatch; absent on attempt 1.
RETRY_NOTE_CONTRACT = (
    "If the bundle carries a 'retry_note', your previous attempt at this task "
    "produced nothing usable for the reason it gives — follow its instruction "
    "instead of repeating that attempt."
)


def _is_header_node(node_id: NodeId) -> bool:
    """Whether an id names a ``module_header`` fragment (imports, ``__future__``)."""
    parts = str(node_id).split("::")
    return len(parts) >= 2 and parts[1] == "module_header"


def _fold_order(
    node_id: NodeId, index: int, order_key: Callable[[NodeId], int | None] | None
) -> tuple[int, int, int]:
    """Sort key placing a folded fragment where it belongs in the rebuilt file.

    Three tiers, because the information available differs per fragment:

    1. a ``module_header`` always leads. It carries the imports, and a
       ``from __future__`` import is only legal as the first statement — so a
       model that emitted its functions before its header produced a file that
       could not compile at all;
    2. otherwise the store's recorded source order, when it knows the id;
    3. otherwise the order the model emitted it in, which is all that is left.
    """
    if _is_header_node(node_id):
        return (0, 0, index)
    known = order_key(node_id) if order_key is not None else None
    if known is not None:
        return (1, known, index)
    return (2, 0, index)


class ReturnedSources(NamedTuple):
    """Where each id an agent returned landed; see :func:`map_returned_sources`."""

    accepted: dict[NodeId, str]
    dropped: list[tuple[NodeId, str]]
    additive: dict[NodeId, str]


# Kinds an agent may create beside a target without a grant. Methods and class
# bodies live inside a class another task may be writing; module-level
# statements have placement semantics a function does not (Wave 28 scope).
ADDITIVE_KINDS = frozenset({"function", "class"})


def additive_candidate(node_id: NodeId, grant: list[NodeId]) -> bool:
    """Whether an out-of-grant id *looks* like a new helper beside a target.

    Syntax only — the id names a top-level function or class in a file where
    the task holds a fragment target. Whether the symbol is really new is the
    store's question, and the session asks it.
    """
    parts = str(node_id).split("::")
    if len(parts) != 3 or parts[1] not in ADDITIVE_KINDS or not parts[2]:
        return False
    fragment_files = {str(n).split("::", 1)[0] for n in grant if "::" in str(n)}
    return parts[0] in fragment_files


def map_returned_sources(
    grant: list[NodeId],
    new_sources: dict[NodeId, str],
    *,
    order_key: Callable[[NodeId], int | None] | None = None,
) -> ReturnedSources:
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
    - it names a new-looking top-level function or class in a file where the
      task holds a fragment target — ``additive`` (:func:`additive_candidate`),
      for the session to accept, escalate or refuse;
    - anything else — dropped, with the reason, for the caller to report. A
      source for a file the task was never granted must not be applied: those
      nodes belong to other tasks and are not write-locked here.
    """
    in_scope = set(grant)
    whole_file_grants = {str(n) for n in grant if "::" not in str(n)}
    accepted: dict[NodeId, str] = {}
    folded: dict[NodeId, list[tuple[tuple[int, int, int], str]]] = {}
    dropped: list[tuple[NodeId, str]] = []
    additive: dict[NodeId, str] = {}
    for index, (node_id, source) in enumerate(new_sources.items()):
        if node_id in in_scope:
            accepted[node_id] = source
            continue
        file_path = str(node_id).split("::", 1)[0]
        if file_path in whole_file_grants:
            folded.setdefault(NodeId(file_path), []).append(
                (_fold_order(node_id, index, order_key), source)
            )
            continue
        if additive_candidate(node_id, grant):
            additive[node_id] = source
            continue
        dropped.append(
            (node_id, "returned node id is outside the task's granted nodes")
        )
    for whole_file_id, parts in folded.items():
        if whole_file_id in accepted:
            continue  # the agent also sent the whole file; that is authoritative
        ordered = [source for _key, source in sorted(parts, key=lambda p: p[0])]
        accepted[whole_file_id] = (
            "\n\n".join(p.strip("\n") for p in ordered) + "\n"
        )
    return ReturnedSources(accepted, dropped, additive)


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


_EXCERPT_LIMIT = 200


def _excerpt(value: object, limit: int = _EXCERPT_LIMIT) -> str:
    """Return a short, single-line, quoted sample of a rejected value.

    A rejection that reports only the *type* it received cannot be diagnosed
    afterwards. One real run failed three times on ``got string`` and the log
    could not say whether the string was a JSON-encoded array (recoverable) or
    raw source text (not), so the fix had to be chosen without the evidence.
    Bounded and whitespace-collapsed, because this lands in an event payload.
    """
    text = value if isinstance(value, str) else repr(value)
    flat = " ".join(text.split())
    return repr(flat if len(flat) <= limit else f"{flat[:limit]}…")


def _parse_fragment_string(raw: str) -> list[Any] | dict[str, Any]:
    """Recover ``modified_fragments`` that arrived JSON-encoded inside a string.

    Never returns a string, so the caller can recurse exactly once.
    """
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, list | dict):
        return decoded
    raise AgentProtocolError(
        "'modified_fragments' must be an array of {node_id, new_source} objects, "
        f"got string: {_excerpt(raw)}"
    )


def _as_fragment_list(raw: object) -> list[dict[str, Any]]:
    """Normalize ``modified_fragments`` to a list of objects.

    The documented shape is an array of ``{node_id, new_source}``. Two schema
    slips are recovered rather than rejected, because both are unambiguous about
    what the model meant and each one cost a real run its whole task subtree:

    - a **single object** instead of an array. Iterating it yields its *keys* —
      strings — so indexing them raised ``TypeError: string indices must be
      integers``, reported as "api call failed", which the retry could do nothing
      with. One object is obviously one fragment.
    - the array **JSON-encoded into a string**. A run lost 1 task and stranded 12
      dependents on three identical ``got string`` rejections, ~18k output tokens
      spent re-earning the same answer. The array is right there; parsing it is
      not a guess.

    A string that is *not* JSON stays a rejection. Reading it as "the source of
    my one target" would mean inventing the node id it belongs to, and this
    decoder does not guess — the retry note states the required shape instead.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return _as_fragment_list(_parse_fragment_string(raw))
    if isinstance(raw, dict):
        return [raw]
    if not isinstance(raw, list):
        raise AgentProtocolError(
            "'modified_fragments' must be an array of {node_id, new_source} "
            f"objects, got {_type_name(raw)}: {_excerpt(raw)}"
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


def _decode_repairs(raw: object) -> int:
    """Return the adapter's repair-turn count, defaulting to 0.

    Adapter telemetry, merged into the payload after the model's own keys — so a
    model that emits a ``repairs`` field of its own cannot forge it. Anything not
    a non-negative integer is read as 0.
    """
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


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
    # The provider's own telemetry is merged into the payload by the adapter
    # before decoding, so it is available even when the *body* turns out to be
    # undecodable. Attaching it to the failure keeps a rejected attempt
    # accountable: without it a malformed reply logs `usage={}`, and the tokens
    # it burned — often the clue to why the model's schema slipped — are lost.
    stop_reason = data.get("stop_reason")
    usage = _decode_usage(data.get("usage"))
    try:
        return _decode_result_body(data)
    except AgentProtocolError as exc:
        if exc.stop_reason is None and not exc.usage:
            exc.stop_reason = stop_reason if isinstance(stop_reason, str) else None
            exc.usage = usage
        raise


def _decode_result_body(data: dict[str, Any]) -> TaskResult:
    """Validate and assemble the result fields; see :func:`decode_task_result`."""
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

    placements: dict[NodeId, NodeId] = {}
    for index, fragment in enumerate(_as_fragment_list(data.get("modified_fragments"))):
        node_id = _fragment_node_id(fragment, index)
        _record(node_id, _fragment_source(fragment, index))
        after = fragment.get("insert_after")
        if isinstance(after, str) and after.strip():
            placements[node_id] = NodeId(after.strip())

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
        repairs=_decode_repairs(data.get("repairs")),
        imports=decode_imports(data.get("imports")),
        needs_nodes=decode_needs(data.get("needs_nodes")),
        placements={**_decode_placements(data.get("placements")), **placements},
    )


def split_import_entry(entry: str) -> tuple[str | None, str]:
    """Split ``"pkg/m.py::import os"`` into its file and statement.

    An entry with no ``<file>.py::`` prefix belongs to whichever file the
    task's targets are in; the session decides which.
    """
    head, sep, rest = entry.partition("::")
    if sep and head.strip().endswith(".py") and " " not in head.strip():
        return head.strip(), rest.strip()
    return None, entry.strip()


def import_statement_error(statement: str) -> str | None:
    """Return why ``statement`` is not one module-level import, or None."""
    try:
        tree = ast.parse(statement)
    except SyntaxError as exc:
        return f"is not valid Python ({exc.msg})"
    if len(tree.body) != 1:
        return f"holds {len(tree.body)} statements, not exactly one import"
    if not isinstance(tree.body[0], ast.Import | ast.ImportFrom):
        return f"is a {type(tree.body[0]).__name__}, not an import statement"
    return None


def decode_imports(raw: object) -> list[str]:
    """Validate the ``imports`` channel: each entry exactly one import statement.

    A malformed entry is a protocol error, not a silent drop, so the adapter's
    repair turn can name it and the model can fix it.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AgentProtocolError(
            f"'imports' must be an array of import statements, got {_type_name(raw)}"
        )
    entries: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, str):
            raise AgentProtocolError(
                f"'imports[{index}]' must be a string, got {_type_name(entry)}"
            )
        _file, statement = split_import_entry(entry)
        problem = import_statement_error(statement)
        if problem is not None:
            raise AgentProtocolError(f"'imports[{index}]' ({entry!r}) {problem}")
        if entry.strip() not in entries:
            entries.append(entry.strip())
    return entries


def decode_needs(raw: object) -> list[NodeNeed]:
    """Validate ``needs_nodes``: objects with a node id, a mode and a reason."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AgentProtocolError(
            f"'needs_nodes' must be an array of objects, got {_type_name(raw)}"
        )
    needs: list[NodeNeed] = []
    for index, item in enumerate(raw):
        entry = _require_mapping(item, f"needs_nodes[{index}]")
        node_id = entry.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise AgentProtocolError(
                f"'needs_nodes[{index}].node_id' must be a non-empty string"
            )
        mode = str(entry.get("mode") or "write").lower()
        if mode not in ("read", "write"):
            raise AgentProtocolError(
                f"'needs_nodes[{index}].mode' must be 'read' or 'write', got {mode!r}"
            )
        reason = entry.get("reason")
        needs.append(NodeNeed(
            NodeId(node_id.strip()), mode, reason if isinstance(reason, str) else ""
        ))
    return needs


def _decode_placements(raw: object) -> dict[NodeId, NodeId]:
    """Read a re-encoded ``placements`` map (id -> the id it follows)."""
    if not isinstance(raw, dict):
        return {}
    return {
        NodeId(str(k)): NodeId(v) for k, v in raw.items() if isinstance(v, str)
    }
