"""The plan schema: parse and validate a JSON plan into ``SubTask`` objects.

Pure parsing with no LLM in sight, shared by the planner (every reply it
accepts), the expansion protocol (a reply that is a plan rather than an expand
request), plan review (an edited plan) and caller-task proposals (which must
re-pass the same invariants). :mod:`mak.planner.planner` re-exports
:func:`parse_plan`, so its long-standing import path still works.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mak.core.exceptions import ContractError
from mak.core.paths import unsafe_node_id_reason
from mak.core.task_codec import subtask_to_dict
from mak.core.types import NodeId, SubTask
from mak.planner.contracts import parse_contract, symbol_of_node
from mak.planner.response import loads_json


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
    # require a whole-file target to be owned by exactly one task. To split work
    # across a file, target distinct symbols (file.py::kind::name) instead.
    whole_file_owner: dict[str, str] = {}
    fragment_files: dict[str, str] = {}  # file path -> a task targeting its fragments
    for task in subtasks:
        for node in dict.fromkeys(task.target_nodes):
            if "::" in node:
                fragment_files.setdefault(target_file(node), task.task_id)
                continue
            if node in whole_file_owner:
                raise ValueError(
                    f"tasks '{whole_file_owner[node]}' and '{task.task_id}' both write "
                    f"the whole file '{node}'; a new file must be created by exactly "
                    "one task. Give each file its own task, or split a file across "
                    "tasks by targeting individual symbols (file.py::kind::name)."
                )
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


def _plan_to_json(tasks: list[SubTask]) -> str:
    """Serialize ``SubTask`` objects to the plan-array JSON ``parse_plan`` accepts."""
    return json.dumps([subtask_to_dict(t) for t in tasks])
