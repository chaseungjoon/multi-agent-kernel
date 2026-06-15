"""Planner: decompose a user task into a validated ``SubTask`` DAG via an LLM.

The planner is the only module that calls an LLM. It builds a prompt
containing the user's task and the current node inventory (qualified names only,
never source), asks the model for a JSON plan, and validates that JSON against the
``SubTask`` schema before accepting it. A malformed or schema-invalid response is
retried up to ``max_retries`` times — each retry feeds the parse error back to the
model — after which ``PlannerFailedError`` is raised.

The LLM is injected as a ``PlannerLLM`` (anything with ``complete(prompt) -> str``)
so the planner is testable with canned responses and is not bound to one SDK.
"""

from __future__ import annotations

import json
from typing import Protocol

from mak.core.exceptions import PlannerFailedError
from mak.core.types import NodeId, SubTask

_PLAN_INSTRUCTIONS = """\
You are the MAK planner. Decompose the user's task into the smallest set of \
independent sub-tasks that can run concurrently, with explicit dependency edges.

Respond with ONLY a JSON array (no prose, no code fences). Each element is an \
object with these keys:
  - "task_id": unique short string id for the sub-task
  - "description": what the sub-task should accomplish
  - "target_nodes": array of node ids this sub-task will WRITE (from the inventory \
below, or new ids for new symbols)
  - "context_nodes": array of node ids this sub-task needs to READ for context \
(sibling methods, class attributes, imports) but will not modify
  - "depends_on": array of task_ids that must complete before this one
  - "agent_type": the agent type to run this sub-task (e.g. "anthropic_api")

MAK edits Python only: every target node id must name a Python source file — either \
"path/to/file.py" or "path/to/file.py::kind::qualified_name". Do NOT target \
non-Python files (no .md, .json, .txt, .js, .html, .css, README, or doc/architecture \
files) — MAK cannot represent them. If the task implies documentation or other \
non-Python artifacts, leave them out of the plan.

Decompose by FILE for a new project: give each new file its own sub-task with that \
file as a bare-path target ("pkg/foo.py"). Never have two sub-tasks both write the \
same whole file — that overwrites work. Prefer many small, focused modules over one \
giant file, and depend on a file only when you truly need its symbols. To split one \
file across sub-tasks, target individual symbols ("pkg/foo.py::function::name"); \
otherwise one file = one task.

Only assign two sub-tasks to write the same node if one depends on the other.

CRITICAL — CASCADE PREVENTION: If ANY sub-task changes a function's public
signature (rename, add, remove, or reorder parameters; change the return type or
default values), you MUST also include sub-tasks for EVERY node that calls that
function — even across different files. Scan the entire inventory before finalising
your plan. An incomplete plan that leaves callers with a stale signature forces a
costly follow-up wave; this is a planning failure. When uncertain whether a caller
exists, include a fix-up task anyway: a no-op task is far cheaper than a broken
codebase. Search the inventory for any node whose name suggests it calls a symbol
you are changing, and include it as a target."""


class PlannerLLM(Protocol):
    """Minimal LLM interface the planner needs: a prompt-in, text-out call."""

    def complete(self, prompt: str) -> str:
        """Return the model's text completion for ``prompt``."""
        ...


def _strip_code_fences(text: str) -> str:
    """Remove a surrounding ```json ... ``` (or ``` ... ```) fence if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # Drop the opening fence (``` or ```json) and a trailing fence if present.
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


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

    return SubTask(
        task_id=task_id,
        description=description,
        target_nodes=[NodeId(n) for n in target_nodes],
        context_nodes=[NodeId(n) for n in context_nodes],
        depends_on=depends_on,
        agent_type=agent_type,
    )


def parse_plan(raw: str) -> list[SubTask]:
    """Parse and validate an LLM (or user) plan string into ``SubTask`` objects.

    Accepts a bare JSON array or an object with a ``"subtasks"`` array, optionally
    wrapped in a code fence. Raises ``ValueError`` on any malformed or
    schema-invalid input (so callers can retry or surface a precise reason).
    """
    text = _strip_code_fences(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response was not valid JSON: {exc}") from exc

    if isinstance(data, dict) and "subtasks" in data:
        data = data["subtasks"]
    _require(isinstance(data, list), "plan must be a JSON array of sub-tasks")

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


class Planner:
    """Turns a natural-language task into a validated list of ``SubTask``."""

    def __init__(self, llm: PlannerLLM, *, max_retries: int = 3) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        self._llm = llm
        self._max_retries = max_retries

    def _build_prompt(self, user_task: str, node_inventory: list[NodeId]) -> str:
        inventory = "\n".join(f"  - {nid}" for nid in node_inventory) or "  (empty)"
        return (
            f"{_PLAN_INSTRUCTIONS}\n\n"
            f"USER TASK:\n{user_task}\n\n"
            f"NODE INVENTORY (qualified names you may target):\n{inventory}\n"
        )

    def decompose(
        self, user_task: str, node_inventory: list[NodeId]
    ) -> list[SubTask]:
        """Decompose ``user_task`` into sub-tasks, retrying on invalid LLM output."""
        prompt = self._build_prompt(user_task, node_inventory)
        last_error: Exception | None = None
        for _attempt in range(self._max_retries):
            current = prompt if last_error is None else (
                f"{prompt}\nYour previous response was rejected: {last_error}\n"
                "Return ONLY the corrected JSON array."
            )
            raw = self._llm.complete(current)
            try:
                return parse_plan(raw)
            except ValueError as exc:
                last_error = exc
        raise PlannerFailedError(
            f"planner failed to produce a valid plan after {self._max_retries} "
            f"attempts: {last_error}"
        )
