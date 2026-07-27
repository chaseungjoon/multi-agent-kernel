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
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol, TypeVar

from mak.core.exceptions import PlannerFailedError
from mak.core.types import NodeId, SubTask
from mak.planner.response import ResponseError, TruncatedResponseError, loads_json

_T = TypeVar("_T")

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


_OUTLINE_INSTRUCTIONS = """\
You are the MAK planner working in OUTLINE mode. Sketch the task at the FILE level \
first — a short ordered list of steps, each naming the files it touches. Detail comes \
in a later pass, so keep each step coarse.

Respond with ONLY a JSON array (no prose, no code fences). Each element is an object \
with these keys:
  - "step_id": unique short string id for the step
  - "description": what this step accomplishes (a later pass expands it into tasks)
  - "files": array of file paths this step touches (from the inventory below, or new \
".py" paths for new files)
  - "depends_on": array of step_ids that must complete before this one

Keep steps independent where you can; only add a "depends_on" edge when a step truly \
needs another step's files to exist or be updated first. Do NOT list non-Python \
files."""

_CRITIQUE_INSTRUCTIONS = """\
You are reviewing a MAK plan you just produced. Look for three defects: missed \
dependency edges (a task edits a node another task's node calls, with no depends_on \
between them), hallucinated node ids (a target that does not match the real code), \
and needless serialization (a depends_on edge with no real code reason).

If the plan is already good, respond with EXACTLY this JSON object and nothing else:
  {"verdict": "ok"}
Otherwise respond with ONLY the corrected full plan as a JSON array in the SAME schema \
as before (task_id, description, target_nodes, context_nodes, depends_on, agent_type). \
Do not add prose or code fences."""


class PlannerLLM(Protocol):
    """Minimal LLM interface the planner needs: a prompt-in, text-out call."""

    def complete(self, prompt: str) -> str:
        """Return the model's text completion for ``prompt``."""
        ...


@dataclass(frozen=True, slots=True)
class _OutlineStep:
    """One file-level step from the outline pass (pre-detail)."""

    step_id: str
    description: str
    files: tuple[str, ...]
    depends_on: tuple[str, ...]


_TRUNCATION_NOTE = """\
Your previous response was cut off by the output-token limit before the JSON \
closed, so it could not be read. Produce a SMALLER plan that fits in one \
response: keep every "description" to one short sentence, merge sub-tasks that \
touch the same file, and emit compact JSON (no pretty-printing, no blank lines, \
no trailing prose). Return ONLY the corrected JSON."""

# Enough of a pause to clear a per-minute rate limit without stalling a run that
# is failing for a reason waiting will not fix.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 8.0


def _backoff_seconds(attempt: int) -> float:
    """Return the delay before retry ``attempt`` (1-based), capped."""
    return float(min(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), _BACKOFF_MAX_SECONDS))


def _retry_note(error: Exception) -> str:
    """Return the feedback appended to the prompt after a failed attempt.

    A truncated response is the one failure that repeats verbatim on a naive
    retry — the same request yields the same over-long plan and the same cut — so
    it gets a note that asks for a smaller plan instead of a corrected one.
    """
    if isinstance(error, TruncatedResponseError):
        return _TRUNCATION_NOTE
    return (
        f"Your previous response was rejected: {error}\n"
        "Return ONLY the corrected JSON."
    )


def _failure_hint(error: Exception | None) -> str:
    """Return actionable advice to append when the retry budget runs out."""
    if isinstance(error, TruncatedResponseError):
        return (
            ". The plan did not fit in the planner model's output budget — narrow "
            "the task, or pick a planner model with a larger output limit."
        )
    return ""


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


def _file_inventory(node_inventory: list[NodeId]) -> dict[str, list[str]]:
    """Group an inventory into ``{file_path: [symbol short names]}`` for the outline."""
    files: dict[str, list[str]] = {}
    for nid in node_inventory:
        path = target_file(nid)
        names = files.setdefault(path, [])
        parts = str(nid).split("::")
        if len(parts) == 3 and parts[2] not in names:
            names.append(parts[2])
    return files


def _inventory_for_files(
    node_inventory: list[NodeId], files: tuple[str, ...]
) -> list[NodeId]:
    """Return the inventory ids whose file is in ``files`` (a new file yields none)."""
    fileset = set(files)
    return [nid for nid in node_inventory if target_file(nid) in fileset]


def _parse_outline(raw: str) -> list[_OutlineStep]:
    """Parse and validate an outline-pass response into ``_OutlineStep`` objects."""
    data = loads_json(raw)
    if isinstance(data, dict) and "steps" in data:
        data = data["steps"]
    if not isinstance(data, list):
        raise ValueError("outline must be a JSON array of steps")

    steps: list[_OutlineStep] = []
    for index, item in enumerate(data):
        where = f"outline step {index}"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be a JSON object")
        steps.append(
            _OutlineStep(
                step_id=_require_str(item.get("step_id"), where, "step_id"),
                description=_require_str(item.get("description"), where, "description"),
                files=tuple(_require_str_list(item.get("files", []), where, "files")),
                depends_on=tuple(
                    _require_str_list(item.get("depends_on", []), where, "depends_on")
                ),
            )
        )

    ids = [s.step_id for s in steps]
    _require(len(ids) == len(set(ids)), "duplicate step_id in outline")
    known = set(ids)
    for step in steps:
        for dep in step.depends_on:
            _require(
                dep in known,
                f"outline step '{step.step_id}' depends on unknown step '{dep}'",
            )
    _require(_outline_is_acyclic(steps), "outline has a dependency cycle")
    return steps


def _outline_is_acyclic(steps: list[_OutlineStep]) -> bool:
    """Return whether the outline's ``depends_on`` edges form a DAG (Kahn)."""
    indegree = {s.step_id: len(set(s.depends_on)) for s in steps}
    dependents: dict[str, list[str]] = {s.step_id: [] for s in steps}
    for step in steps:
        for dep in set(step.depends_on):
            dependents[dep].append(step.step_id)
    ready = [sid for sid, deg in indegree.items() if deg == 0]
    seen = 0
    while ready:
        node = ready.pop()
        seen += 1
        for child in dependents[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return seen == len(steps)


def _namespace_tasks(tasks: list[SubTask], index: int) -> list[SubTask]:
    """Prefix a step's task ids with ``s<index>.`` and remap intra-step deps."""
    prefix = f"s{index}."
    local = {t.task_id for t in tasks}
    return [
        replace(
            task,
            task_id=prefix + task.task_id,
            depends_on=[
                prefix + dep if dep in local else dep for dep in task.depends_on
            ],
        )
        for task in tasks
    ]


def _assemble_outline(
    steps: list[_OutlineStep], step_tasks: dict[str, list[SubTask]]
) -> list[SubTask]:
    """Concatenate step tasks, adding an edge from each upstream step's tasks."""
    ids_by_step = {
        sid: [t.task_id for t in tasks] for sid, tasks in step_tasks.items()
    }
    merged: list[SubTask] = []
    for step in steps:
        upstream: list[str] = []
        for dep_step in step.depends_on:
            upstream.extend(ids_by_step.get(dep_step, []))
        for task in step_tasks[step.step_id]:
            extra = [u for u in upstream if u not in task.depends_on]
            merged.append(replace(task, depends_on=list(task.depends_on) + extra))
    return merged


def _plan_to_json(tasks: list[SubTask]) -> str:
    """Serialize ``SubTask`` objects to the plan-array JSON ``parse_plan`` accepts."""
    return json.dumps(
        [
            {
                "task_id": t.task_id,
                "description": t.description,
                "target_nodes": [str(n) for n in t.target_nodes],
                "context_nodes": [str(n) for n in t.context_nodes],
                "depends_on": list(t.depends_on),
                "agent_type": t.agent_type,
            }
            for t in tasks
        ]
    )


class Planner:
    """Turns a natural-language task into a validated list of ``SubTask``."""

    def __init__(
        self,
        llm: PlannerLLM,
        *,
        max_retries: int = 3,
        agent_types: list[str] | None = None,
        strategy: str = "oneshot",
        self_critique: bool = False,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        self._llm = llm
        self._max_retries = max_retries
        # The agent types actually configured for this run, so the plan can name a
        # real one in each task's "agent_type" instead of guessing (an unconfigured
        # type would otherwise have to be remapped by the session).
        self._agent_types = list(agent_types or [])
        self._strategy = strategy
        self._self_critique = self_critique

    def _build_prompt(self, user_task: str, node_inventory: list[NodeId]) -> str:
        inventory = "\n".join(f"  - {nid}" for nid in node_inventory) or "  (empty)"
        if self._agent_types:
            agents = (
                "\nCONFIGURED AGENT TYPES (set each task's \"agent_type\" to one of "
                "these, or leave it empty to let MAK distribute the work):\n"
                + "\n".join(f"  - {t}" for t in self._agent_types)
                + "\n"
            )
        else:
            agents = ""
        return (
            f"{_PLAN_INSTRUCTIONS}\n\n"
            f"USER TASK:\n{user_task}\n\n"
            f"NODE INVENTORY (qualified names you may target):\n{inventory}\n"
            f"{agents}"
        )

    def decompose(
        self, user_task: str, node_inventory: list[NodeId]
    ) -> list[SubTask]:
        """Decompose ``user_task`` into sub-tasks, retrying on invalid LLM output.

        With ``strategy="outline"`` this runs a two-pass plan (file-level outline,
        then per-step detail); the default ``"oneshot"`` is a single decomposition
        call. Either way the merged result is validated by ``parse_plan``, and when
        ``self_critique`` is set one reflection pass may replace it.
        """
        if self._strategy == "outline":
            plan = self._decompose_outline(user_task, node_inventory)
        else:
            prompt = self._build_prompt(user_task, node_inventory)
            plan = self._complete_with_retries(prompt, parse_plan)
        if self._self_critique:
            plan = self._critique_plan(plan)
        return plan

    def _critique_plan(self, plan: list[SubTask]) -> list[SubTask]:
        """Run one reflection pass; adopt a corrected plan or keep the original.

        A broken critique must never break a good plan: only a ``verdict: ok`` reply
        or a plan that re-parses cleanly is honored — anything else keeps ``plan``.
        No retry budget is consumed.
        """
        prompt = f"{_CRITIQUE_INSTRUCTIONS}\n\nPLAN:\n{_plan_to_json(plan)}\n"
        # The critique is an optional improvement pass, so *any* failure in it —
        # a dead API, a truncated reply, an unparseable plan — must leave the
        # already-valid plan standing rather than take the run down.
        try:
            raw = self._llm.complete(prompt)
            data = loads_json(raw)
        except Exception:  # noqa: BLE001 - see comment above
            return plan
        if isinstance(data, dict) and data.get("verdict") == "ok":
            return plan
        try:
            return parse_plan(raw)
        except ValueError:
            return plan

    def _complete_with_retries(
        self, prompt: str, parse: Callable[[str], _T]
    ) -> _T:
        """Call the LLM until ``parse`` accepts a response, feeding back errors.

        Both halves of an attempt are retried: a transient provider failure (a
        rate limit, a dropped connection) is as recoverable as a malformed reply,
        and previously it aborted the run outright with the retry budget untouched.
        Only a failed *call* backs off — a rejected plan is re-asked immediately,
        since waiting does nothing to make the model answer better.
        """
        last_error: Exception | None = None
        call_failed = False
        for attempt in range(self._max_retries):
            if call_failed:
                self._sleep(_backoff_seconds(attempt))
            current = (
                prompt if last_error is None else f"{prompt}\n{_retry_note(last_error)}"
            )
            try:
                raw = self._llm.complete(current)
            except PlannerFailedError:
                # A setup failure (missing SDK, unknown backend) is not transient;
                # retrying it just delays the same message.
                raise
            except ResponseError as exc:
                # A provider-signalled bad response (a cut, a blocked candidate)
                # is a response problem, not a transport one — no backoff.
                last_error, call_failed = exc, False
                continue
            except Exception as exc:  # noqa: BLE001 - provider SDKs raise freely
                last_error, call_failed = exc, True
                continue
            call_failed = False
            try:
                return parse(raw)
            except ValueError as exc:
                last_error = exc
        raise PlannerFailedError(
            f"planner failed to produce a valid plan after {self._max_retries} "
            f"attempts: {last_error}{_failure_hint(last_error)}"
        )

    @staticmethod
    def _sleep(seconds: float) -> None:
        """Pause between attempts (a seam so tests do not wait)."""
        time.sleep(seconds)

    def _decompose_outline(
        self, user_task: str, node_inventory: list[NodeId]
    ) -> list[SubTask]:
        """Two-pass plan: a file-level outline, then per-step symbol detail."""
        outline_prompt = self._build_outline_prompt(user_task, node_inventory)
        steps = self._complete_with_retries(outline_prompt, _parse_outline)

        step_tasks: dict[str, list[SubTask]] = {}
        for index, step in enumerate(steps):
            restricted = _inventory_for_files(node_inventory, step.files)
            detail_prompt = self._build_prompt(step.description, restricted)
            tasks = self._complete_with_retries(detail_prompt, parse_plan)
            step_tasks[step.step_id] = _namespace_tasks(tasks, index)

        merged = _assemble_outline(steps, step_tasks)
        # Re-run the full-plan invariants (duplicate ids, whole-file owners) over the
        # assembled result the same way an LLM plan would be checked.
        return parse_plan(_plan_to_json(merged))

    def _build_outline_prompt(
        self, user_task: str, node_inventory: list[NodeId]
    ) -> str:
        files = _file_inventory(node_inventory)
        if files:
            listed = "\n".join(
                f"  - {path}: {', '.join(names) or '(no symbols)'}"
                for path, names in files.items()
            )
        else:
            listed = "  (empty)"
        return (
            f"{_OUTLINE_INSTRUCTIONS}\n\n"
            f"USER TASK:\n{user_task}\n\n"
            f"FILE INVENTORY (files you may touch, with their symbols):\n{listed}\n"
        )
