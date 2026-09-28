"""The plan schema: parse and validate a JSON plan into ``SubTask`` objects.

Pure parsing with no LLM in sight, shared by the planner (every reply it
accepts), the expansion protocol (a reply that is a plan rather than an expand
request), plan review (an edited plan) and caller-task proposals (which must
re-pass the same invariants). :mod:`mak.planner.planner` re-exports
:func:`parse_plan`, so its long-standing import path still works.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace

from mak.core.exceptions import ContractError
from mak.core.paths import unsafe_node_id_reason
from mak.core.task_codec import subtask_to_dict
from mak.core.types import NodeId, SubTask
from mak.planner.contracts import parse_contract, symbol_of_node
from mak.planner.response import loads_json

_logger = logging.getLogger(__name__)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def target_file(node_id: str) -> str:
    """Return a node id's file-path component (``a.py::function::f`` -> ``a.py``)."""
    return node_id.split("::", 1)[0]


def is_python_target(node_id: str) -> bool:
    """Return whether a target node id names a Python (``.py``) source file."""
    return target_file(node_id).endswith(".py")


def _require_str(value: object, where: str, field_name: str) -> str:
    """Return ``value`` as a non-empty string or raise ``ValueError``."""
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{where}: '{field_name}' must be a non-empty string")
    return value


def _require_str_list(value: object, where: str, field_name: str) -> list[str]:
    """Return ``value`` as a list of strings or raise ``ValueError``."""
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise ValueError(f"{where}: '{field_name}' must be a list of strings")
    return list(value)


def _coerce_subtask(raw: object, index: int) -> SubTask:
    if not isinstance(raw, dict):
        raise ValueError(f"sub-task {index} must be a JSON object")
    where = f"sub-task {index}"

    task_id = _require_str(raw.get("task_id"), where, "task_id")
    description = _require_str(raw.get("description"), where, "description")
    target_nodes = _require_str_list(raw.get("target_nodes", []), where, "target_nodes")
    context_nodes = _require_str_list(
        raw.get("context_nodes", []), where, "context_nodes"
    )
    depends_on = _require_str_list(raw.get("depends_on", []), where, "depends_on")

    agent_type = raw.get("agent_type", "")
    if not isinstance(agent_type, str):
        raise ValueError(f"{where}: 'agent_type' must be a string")

    declared = _coerce_declarations(raw, where, target_nodes)
    return SubTask(
        task_id=task_id,
        description=description,
        target_nodes=[NodeId(n) for n in target_nodes],
        context_nodes=[NodeId(n) for n in context_nodes],
        depends_on=depends_on,
        agent_type=agent_type,
        changes_api=declared.changes_api,
        api_targets=declared.api_targets,
        contract=declared.contract,
        registry_keys=declared.registry_keys,
    )


@dataclass(frozen=True, slots=True)
class _Declarations:
    """A sub-task's interface declarations, validated."""

    changes_api: bool | None
    api_targets: list[NodeId]
    contract: dict[NodeId, str]
    registry_keys: dict[NodeId, list[str]]


def _coerce_declarations(
    raw: dict[str, object], where: str, targets: list[str]
) -> _Declarations:
    """Validate the optional interface declarations against the task's targets.

    Each declaration is a promise the kernel enforces at commit, so one that
    cannot be honoured — an API target the task does not write, a contract that
    is not a signature, "body-only" alongside a contract that changes the API —
    is refused here, where the planner can be asked again, rather than failing
    a commit later.
    """
    changes_api = raw.get("changes_api")
    if changes_api is not None and not isinstance(changes_api, bool):
        raise ValueError(f"{where}: 'changes_api' must be true, false, or null")
    api_targets = _require_str_list(raw.get("api_targets", []), where, "api_targets")
    contract = _require_str_map(raw.get("contract", {}), where, "contract")
    keys = _require_key_map(raw.get("registry_keys", {}), where)
    owned = set(targets)
    for field_name, ids in (
        ("api_targets", api_targets),
        ("contract", list(contract)),
        ("registry_keys", list(keys)),
    ):
        stray = [n for n in ids if n not in owned]
        if stray:
            raise ValueError(
                f"{where}: '{field_name}' names node(s) the task does not target: "
                f"{', '.join(stray)} — declare only on this task's target_nodes"
            )
    for node_id, text in contract.items():
        _check_contract(where, node_id, text)
    if changes_api is False and (api_targets or contract):
        raise ValueError(
            f"{where}: 'changes_api' is false but the task declares "
            "'api_targets' or a 'contract' — a contract creates or changes an API"
        )
    if changes_api is None and (api_targets or contract):
        changes_api = True  # declaring an API target *is* declaring a change
    return _Declarations(
        changes_api=changes_api,
        api_targets=[NodeId(n) for n in api_targets],
        contract={NodeId(k): v for k, v in contract.items()},
        registry_keys={NodeId(k): v for k, v in keys.items()},
    )


def _check_contract(where: str, node_id: str, text: str) -> None:
    """Refuse a contract that does not parse or names a different symbol."""
    try:
        contract = parse_contract(text)
    except ContractError as exc:
        raise ValueError(f"{where}: {exc}") from exc
    symbol = symbol_of_node(node_id)
    if symbol is not None and symbol != contract.name:
        raise ValueError(
            f"{where}: contract for '{node_id}' declares '{contract.name}', "
            f"but that node is '{symbol}'"
        )


def _require_str_map(value: object, where: str, field_name: str) -> dict[str, str]:
    """Return ``value`` as a ``{str: non-empty str}`` mapping or raise."""
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v.strip()
        for k, v in value.items()
    ):
        raise ValueError(
            f"{where}: '{field_name}' must map node ids to non-empty strings"
        )
    return dict(value)


def _require_key_map(value: object, where: str) -> dict[str, list[str]]:
    """Return ``registry_keys`` as ``{node id: [key, ...]}`` or raise."""
    if not isinstance(value, dict) or not all(
        isinstance(k, str)
        and isinstance(v, list)
        and all(isinstance(key, str) and key for key in v)
        for k, v in value.items()
    ):
        raise ValueError(
            f"{where}: 'registry_keys' must map node ids to lists of keys"
        )
    return {k: list(dict.fromkeys(v)) for k, v in value.items()}


def parse_plan(raw: str) -> list[SubTask]:
    """Parse and validate an LLM (or user) plan string into ``SubTask`` objects.

    Accepts a bare JSON array or an object with a ``"subtasks"`` array, optionally
    wrapped in a code fence or framed by prose. Raises ``ValueError`` on any
    malformed or schema-invalid input (so callers can retry or surface a precise
    reason), and ``TruncatedResponseError`` when the response was cut short.
    """
    data = loads_json(raw)

    if isinstance(data, dict) and "subtasks" in data:
        data = data["subtasks"]
    if not isinstance(data, list):
        raise ValueError("plan must be a JSON array of sub-tasks")

    subtasks = [_coerce_subtask(item, i) for i, item in enumerate(data)]

    ids = [t.task_id for t in subtasks]
    _require(len(ids) == len(set(ids)), "duplicate task_id in plan")
    known = set(ids)
    for task in subtasks:
        for dep in task.depends_on:
            _require(
                dep in known,
                f"sub-task '{task.task_id}' depends on unknown task '{dep}'",
            )

    # A target's file component becomes a real filesystem path twice over (the
    # node store's fragment dir, the reconstructed file under the work dir), and
    # both joins are unsafe for an absolute or ".."-bearing id. Checked before the
    # ".py" rule below because containment is the more fundamental property: an
    # id like "/etc/cron.d/payload.py" satisfies the extension rule perfectly.
    #
    # Raised as ValueError, deliberately: _complete_with_retries catches it and
    # feeds the reason back to the model, so a hallucinated path is re-asked
    # rather than taking the run down.
    unsafe = [
        (task.task_id, str(node), reason)
        for task in subtasks
        for node in task.target_nodes
        if (reason := unsafe_node_id_reason(str(node))) is not None
    ]
    if unsafe:
        listed = "; ".join(f"{tid} -> {node} ({why})" for tid, node, why in unsafe)
        raise ValueError(
            "every target_node must name a file inside the working directory, but "
            f"these do not: {listed}. Use a project-relative path such as "
            "'pkg/module.py' or 'pkg/module.py::kind::name'."
        )

    # MAK can only represent Python AST nodes — a non-".py" target can never be
    # ingested, validated, or reconstructed, so reject it here with a clear reason
    # instead of failing cryptically deep in the parser at commit time.
    bad = [
        (task.task_id, node)
        for task in subtasks
        for node in task.target_nodes
        if not is_python_target(node)
    ]
    if bad:
        listed = "; ".join(f"{tid} -> {node}" for tid, node in bad)
        raise ValueError(
            "MAK only edits Python (.py) nodes, but these targets name non-Python "
            f"files: {listed}. Use 'path/to/file.py' or "
            "'path/to/file.py::kind::name' for every target_node, and drop tasks that "
            "produce documentation or other non-Python artifacts."
        )

    # A *whole-file* target (a bare 'path.py' with no ::kind::name) is the entire
    # file. If two tasks each return a whole file, the second clobbers the first, so
    # a whole-file target must be owned by exactly one task. Tasks sharing one are
    # merged into a single task rather than bounced back to the planner: a small
    # model asked to fix it tends to re-send the same plan until retries run out.
    # Only a merge that would create a dependency cycle is still refused.
    subtasks = _merge_whole_file_owners(subtasks)
    whole_file_owner: dict[str, str] = {}
    fragment_files: dict[str, str] = {}  # file path -> a task targeting its fragments
    for task in subtasks:
        for node in dict.fromkeys(task.target_nodes):
            if "::" in node:
                fragment_files.setdefault(target_file(node), task.task_id)
                continue
            if node in whole_file_owner:
                raise ValueError(_whole_file_conflict(
                    whole_file_owner[node], task.task_id, node
                ))
            whole_file_owner[node] = task.task_id

    # A file cannot be edited at *both* granularities in one plan: a whole-file commit
    # supersedes that file's fragment nodes, so a sibling fragment task would lose its
    # work (or double symbols, depending on order). Pick one granularity per file.
    mixed = sorted(set(whole_file_owner) & set(fragment_files))
    if mixed:
        listed = "; ".join(
            f"'{f}' (whole: {whole_file_owner[f]}, fragment: {fragment_files[f]})"
            for f in mixed
        )
        raise ValueError(
            "a file is targeted both as a whole file and by individual symbols, which "
            f"would lose work when the whole-file write supersedes its fragments: "
            f"{listed}. Edit each file at one granularity — either one whole-file task "
            "or only 'file.py::kind::name' symbol tasks."
        )
    return subtasks


def _whole_file_conflict(first: str, second: str, node: str) -> str:
    """Return the retry feedback for two tasks writing the whole file ``node``."""
    return (
        f"tasks '{first}' and '{second}' both write the whole file '{node}', and "
        "a whole file can have only one writer. Merge them into one task that "
        f"targets '{node}', or, if the inventory lists symbols for that file, "
        "split the work by targeting individual symbols (file.py::kind::name)."
    )


def _merge_whole_file_owners(subtasks: list[SubTask]) -> list[SubTask]:
    """Fold tasks that share a whole-file target into one task per group.

    Groups are transitive (a shares ``x.py`` with b, b shares ``y.py`` with c:
    one task). The merged task keeps the first member's id and position; every
    other task's ``depends_on`` is remapped onto it. Raises ``ValueError`` when
    the merge would close a dependency cycle — some task outside the group both
    depends on one member and is depended on by another.
    """
    parent = list(range(len(subtasks)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owners: dict[str, list[int]] = {}
    for i, task in enumerate(subtasks):
        for node in dict.fromkeys(str(n) for n in task.target_nodes):
            if "::" not in node:
                owners.setdefault(node, []).append(i)
    for indices in owners.values():
        for i in indices[1:]:
            parent[find(i)] = find(indices[0])
    groups: dict[int, list[int]] = {}
    for i in range(len(subtasks)):
        groups.setdefault(find(i), []).append(i)
    if all(len(members) == 1 for members in groups.values()):
        return subtasks

    head = {
        subtasks[i].task_id: subtasks[min(members)].task_id
        for members in groups.values()
        for i in members
    }
    merged: list[SubTask] = []
    for i, task in enumerate(subtasks):
        members = groups[find(i)]
        if i != min(members):
            continue
        group = [subtasks[m] for m in members]
        combined = _combine(group) if len(group) > 1 else task
        deps = [head[d] for d in combined.depends_on if head[d] != combined.task_id]
        merged.append(replace(combined, depends_on=list(dict.fromkeys(deps))))

    graph = {t.task_id: t.depends_on for t in merged}
    for root, members in groups.items():
        if len(members) > 1 and _reaches_itself(graph, subtasks[min(members)].task_id):
            node, indices = next(
                (node, indices) for node, indices in owners.items()
                if len(indices) > 1 and find(indices[0]) == root
            )
            first, second = (subtasks[m].task_id for m in indices[:2])
            raise ValueError(
                _whole_file_conflict(first, second, node)
                + " They cannot be merged automatically: another task sits "
                "between them in the dependency order."
            )
    for members in groups.values():
        if len(members) > 1:
            _logger.info(
                "merged tasks %s into '%s': they write the same whole file",
                ", ".join(repr(subtasks[m].task_id) for m in members),
                subtasks[min(members)].task_id,
            )
    return merged


def _combine(group: list[SubTask]) -> SubTask:
    """One task doing the work of ``group``, which shares whole-file targets."""
    first = group[0]
    ids = ", ".join(f"'{t.task_id}'" for t in group)
    description = (
        f"This task combines {ids}, which write the same file(s). Do all of:\n\n"
        + "\n\n".join(f"[{t.task_id}] {t.description}" for t in group)
    )

    def union(lists: list[list[NodeId]]) -> list[NodeId]:
        return list(dict.fromkeys(n for nodes in lists for n in nodes))

    targets = union([t.target_nodes for t in group])
    context = [n for n in union([t.context_nodes for t in group]) if n not in targets]
    contract: dict[NodeId, str] = {}
    registry_keys: dict[NodeId, list[str]] = {}
    for t in group:
        contract.update(t.contract)
        for node, keys in t.registry_keys.items():
            merged_keys = [*registry_keys.get(node, []), *keys]
            registry_keys[node] = list(dict.fromkeys(merged_keys))

    # changes_api is tri-state; the merge must stay at least as conservative as
    # every member. All False: a body-only promise holds. All None: still
    # unknown. Otherwise a declared change, narrowed to the targets each member
    # could change (None or a bare True means all of its targets).
    flags = {t.changes_api for t in group}
    changes_api: bool | None
    api_targets: list[NodeId] = []
    if flags == {False}:
        changes_api = False
    elif flags == {None}:
        changes_api = None
    else:
        changes_api = True
        api_targets = union([
            (t.api_targets or t.target_nodes) if t.changes_api is not False else []
            for t in group
        ])
    return replace(
        first,
        description=description,
        target_nodes=targets,
        context_nodes=context,
        depends_on=[d for t in group for d in t.depends_on],
        changes_api=changes_api,
        api_targets=api_targets,
        contract=contract,
        registry_keys=registry_keys,
    )


def _reaches_itself(deps: dict[str, list[str]], start: str) -> bool:
    """Whether ``start`` transitively depends on itself."""
    seen: set[str] = set()
    stack = list(deps.get(start, ()))
    while stack:
        tid = stack.pop()
        if tid == start:
            return True
        if tid not in seen:
            seen.add(tid)
            stack.extend(deps.get(tid, ()))
    return False


def _plan_to_json(tasks: list[SubTask]) -> str:
    """Serialize ``SubTask`` objects to the plan-array JSON ``parse_plan`` accepts."""
    return json.dumps([subtask_to_dict(t) for t in tasks])
