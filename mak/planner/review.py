"""Human-in-the-loop review of a planner-generated DAG.

Before a plan is dispatched to the scheduler, the user sees the sub-task list and
its dependency edges and chooses to **approve**, **edit** (paste a corrected JSON
plan), or **abort**. A bad plan — a missed dependency or a hallucinated edge —
causes agent collisions or needless serialization that are expensive to unwind
mid-session, so this ~5-second check removes the single-point-of-failure risk of
one-shot LLM DAG generation.

I/O is injected (``prompt_fn`` / ``printer``) so the flow is fully testable and the
caller can bypass review entirely (the ``--no-review`` path simply does not call
``display_plan_for_review``).
"""

from __future__ import annotations

from collections.abc import Callable

from mak.core.exceptions import PlanReviewAborted
from mak.core.types import SubTask
from mak.planner.planner import parse_plan


def render_plan(subtasks: list[SubTask]) -> str:
    """Render the sub-task list and dependency edges as human-readable text."""
    if not subtasks:
        return "(empty plan — no sub-tasks)"
    lines: list[str] = ["Proposed plan:", ""]
    for task in subtasks:
        targets = ", ".join(task.target_nodes) if task.target_nodes else "(none)"
        agent = task.agent_type or "(default)"
        lines.append(f"  [{task.task_id}] {task.description}")
        lines.append(f"        agent={agent}  writes={targets}")
    lines.append("")
    lines.append("Dependency edges:")
    edges = [
        f"  {dep} -> {task.task_id}"
        for task in subtasks
        for dep in task.depends_on
    ]
    lines.extend(edges or ["  (none — all sub-tasks are independent)"])
    return "\n".join(lines)


def display_plan_for_review(
    subtasks: list[SubTask],
    *,
    header: str | None = None,
    prompt_fn: Callable[[str], str] = input,
    printer: Callable[[str], None] = print,
) -> list[SubTask]:
    """Show the plan and return the approved (possibly edited) sub-task list.

    ``header`` is printed before the plan when present — used by cascade waves
    to explain why these extra tasks appeared.

    Returns the original list on approval, or a re-parsed list on edit. Raises
    ``PlanReviewAborted`` if the user aborts.
    """
    if header:
        printer(header)
        printer("")
    printer(render_plan(subtasks))
    while True:
        answer = prompt_fn("Approve plan? [a]pprove / [e]dit / a[b]ort: ")
        choice = answer.strip().lower()
        if choice in ("", "a", "approve"):
            return subtasks
        if choice in ("b", "abort", "q"):
            raise PlanReviewAborted("plan review aborted by user")
        if choice in ("e", "edit"):
            edited = _prompt_for_edit(prompt_fn=prompt_fn, printer=printer)
            if edited is not None:
                return edited
            continue
        printer(f"Unrecognized choice: {choice!r}. Please pick a, e, or b.")


def _prompt_for_edit(
    *,
    prompt_fn: Callable[[str], str],
    printer: Callable[[str], None],
) -> list[SubTask] | None:
    """Prompt for a replacement JSON plan; return it, or None to re-show the menu."""
    raw = prompt_fn(
        "Paste the corrected plan as a JSON array (or blank to cancel): "
    )
    if raw.strip() == "":
        return None
    try:
        edited = parse_plan(raw)
    except ValueError as exc:
        printer(f"Edited plan rejected: {exc}")
        return None
    printer(render_plan(edited))
    return edited
