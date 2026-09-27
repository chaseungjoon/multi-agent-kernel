"""The ``outline`` strategy's parsing and assembly (the prompts live in the planner).

``outline`` plans in two passes: a file-level outline of steps, then one detail
call per step over only that step's files. These helpers parse and check the
outline, restrict the inventory per step, and merge the step plans back into
one plan whose ids and cross-step edges are unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from mak.core.types import NodeId, SubTask
from mak.planner.plan_schema import (
    _require,
    _require_str,
    _require_str_list,
    target_file,
)
from mak.planner.response import loads_json


@dataclass(frozen=True, slots=True)
class OutlineStep:
    """One file-level step from the outline pass (pre-detail)."""

    step_id: str
    description: str
    files: tuple[str, ...]
    depends_on: tuple[str, ...]


def file_inventory(node_inventory: list[NodeId]) -> dict[str, list[str]]:
    """Group an inventory into ``{file_path: [symbol short names]}`` for the outline."""
    files: dict[str, list[str]] = {}
    for nid in node_inventory:
        path = target_file(nid)
        names = files.setdefault(path, [])
        parts = str(nid).split("::")
        if len(parts) == 3 and parts[2] not in names:
            names.append(parts[2])
    return files


def inventory_for_files(
    node_inventory: list[NodeId], files: tuple[str, ...]
) -> list[NodeId]:
    """Return the inventory ids whose file is in ``files`` (a new file yields none)."""
    fileset = set(files)
    return [nid for nid in node_inventory if target_file(nid) in fileset]


def parse_outline(raw: str) -> list[OutlineStep]:
    """Parse and validate an outline-pass response into ``OutlineStep`` objects."""
    data = loads_json(raw)
    if isinstance(data, dict) and "steps" in data:
        data = data["steps"]
    if not isinstance(data, list):
        raise ValueError("outline must be a JSON array of steps")

    steps: list[OutlineStep] = []
    for index, item in enumerate(data):
        where = f"outline step {index}"
        if not isinstance(item, dict):
            raise ValueError(f"{where} must be a JSON object")
        steps.append(
            OutlineStep(
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
    _require(outline_is_acyclic(steps), "outline has a dependency cycle")
    return steps


def outline_is_acyclic(steps: list[OutlineStep]) -> bool:
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


def namespace_tasks(tasks: list[SubTask], index: int) -> list[SubTask]:
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


def assemble_outline(
    steps: list[OutlineStep], step_tasks: dict[str, list[SubTask]]
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


def outline_listing(node_inventory: list[NodeId]) -> str:
    """Render the outline pass's ``path: symbols`` inventory."""
    files = file_inventory(node_inventory)
    if not files:
        return "  (empty)"
    return "\n".join(
        f"  - {path}: {', '.join(names) or '(no symbols)'}"
        for path, names in files.items()
    )
