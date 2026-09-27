"""Human-in-the-loop review of a planner-generated DAG.

Before a plan is dispatched to the scheduler, the user sees the sub-task list and
its dependency edges and chooses to **approve**, **edit** (paste a corrected JSON
plan), or **abort** — and, when MAK added caller-update tasks for the plan's
signature changes (marked ``[proposed by MAK]``), to **drop** them. A bad plan —
a missed dependency or a hallucinated edge — causes agent collisions or needless
serialization that are expensive to unwind mid-session, so this ~5-second check
removes the single-point-of-failure risk of one-shot LLM DAG generation.

I/O is injected (``prompt_fn`` / ``printer``) so the flow is fully testable and the
caller can bypass review entirely (the ``--no-review`` path simply does not call
``display_plan_for_review``).
"""

from __future__ import annotations

from collections.abc import Callable

from mak.core.exceptions import PlanReviewAborted
from mak.core.types import SubTask
from mak.planner.callers import drop_tasks
from mak.planner.planner import parse_plan
from mak.planner.validation import PlanFinding

# Findings that represent an automatic change validation made (shown with ✎),
# vs. those that are advisory only and need the reviewer's judgement (shown with ⚠).
# A finding whose message starts "added:" is applied too — an edge validation
# added, or a caller MAK proposed a task for.
_APPLIED_KINDS = frozenset({"corrected_node", "context_dropped"})
PROPOSED_MARK = "[proposed by MAK]"


def is_applied(finding: PlanFinding) -> bool:
    """Whether a finding records a change made to the plan (vs. advice)."""
    return finding.kind in _APPLIED_KINDS or "added:" in finding.message


def _render_findings(findings: list[PlanFinding]) -> list[str]:
    """Render validation findings: ✎ for applied changes, ⚠ for suggestions."""
    lines = ["", "Plan validation:"]
    for finding in findings:
        mark = "✎" if is_applied(finding) else "⚠"
        suffix = (
            f"  (candidates: {', '.join(finding.suggestions)})"
            if finding.suggestions else ""
        )
        lines.append(f"  {mark} [{finding.task_id}] {finding.message}{suffix}")
    return lines


def render_plan(
    subtasks: list[SubTask],
    findings: list[PlanFinding] | None = None,
    proposed: frozenset[str] = frozenset(),
) -> str:
    """Render the sub-task list, dependency edges, and any validation findings.

    Tasks in ``proposed`` — the caller updates MAK added — are marked
    ``[proposed by MAK]``.
    """
    if not subtasks:
        return "(empty plan — no sub-tasks)"
    lines: list[str] = ["Proposed plan:", ""]
    for task in subtasks:
        targets = ", ".join(task.target_nodes) if task.target_nodes else "(none)"
        agent = task.agent_type or "(default)"
        mark = f" {PROPOSED_MARK}" if task.task_id in proposed else ""
        lines.append(f"  [{task.task_id}]{mark} {task.description}")
        lines.append(f"        agent={agent}  writes={targets}")
        if task.repair_obligations:
            repairs = "; ".join(
                obligation.detail for obligation in task.repair_obligations
            )
            lines.append(f"        must resolve={repairs}")
    lines.append("")
    lines.append("Dependency edges:")
    edges = [
        f"  {dep} -> {task.task_id}"
        for task in subtasks
        for dep in task.depends_on
    ]
    lines.extend(edges or ["  (none — all sub-tasks are independent)"])
    if findings:
        lines.extend(_render_findings(findings))
    return "\n".join(lines)


def display_plan_for_review(
    subtasks: list[SubTask],
    *,
    header: str | None = None,
    findings: list[PlanFinding] | None = None,
    prompt_fn: Callable[[str], str] = input,
    printer: Callable[[str], None] = print,
    proposed: frozenset[str] = frozenset(),
) -> list[SubTask]:
    """Show the plan and return the approved (possibly edited) sub-task list.

    ``header`` is printed before the plan when present — used by cascade waves
    to explain why these extra tasks appeared. ``findings`` are the deterministic
    validation observations (corrections applied, edges added, suggestions),
    rendered under the plan so the reviewer sees what validation did.

    ``proposed`` names the caller-update tasks MAK added. When there are any,
    a ``[d]rop`` choice removes them all, re-renders the plan and asks again;
    ``install_plan`` then reports their callers as ``missing_caller`` findings
    and does not re-add them.

    Returns the original list on approval, or a re-parsed list on edit. Raises
    ``PlanReviewAborted`` if the user aborts.
    """
    if header:
        printer(header)
        printer("")
    printer(render_plan(subtasks, findings, proposed))
    while True:
        answer = prompt_fn(_menu(proposed))
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
        if proposed and choice in ("d", "drop"):
            subtasks = drop_tasks(subtasks, proposed)
            printer(
                f"Dropped {len(proposed)} MAK-proposed task(s); their callers "
                "will be reported as missing_caller when the plan is installed."
            )
            proposed = frozenset()
            printer(render_plan(subtasks))
            continue
        letters = "a, e, d, or b" if proposed else "a, e, or b"
        printer(f"Unrecognized choice: {choice!r}. Please pick {letters}.")


def _menu(proposed: frozenset[str]) -> str:
    """Return the review prompt, offering to drop MAK's tasks when there are any."""
    if proposed:
        return (
            "Approve plan? [a]pprove / [e]dit / [d]rop MAK-proposed tasks / "
            "a[b]ort: "
        )
    return "Approve plan? [a]pprove / [e]dit / a[b]ort: "


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
