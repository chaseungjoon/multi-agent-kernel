"""Caller completion: the kernel supplies the callers a signature change breaks.

The planner used to be told to *guess* callers — "search the inventory for any
node whose name suggests it calls a symbol you are changing" — while the kernel
held the real reference graph and used it only to repair edges. Here the graph
answers the question directly:

- :func:`find_missing_callers` lists every graph-visible caller of a function
  or method a task declares it is changing (``changes_api: true``) that no task
  in the plan updates. Validation reports each one as a ``missing_caller``
  finding, and one ``undeclared_api_callers`` finding per task that changes
  targets with outside callers without saying whether their API changes.
- :func:`propose_caller_tasks` turns the missing callers into tasks, one per
  caller *file*. It runs only on the planner's path (``PlanPreparer.propose``),
  never inside validation: ``install_plan`` re-validates every plan, and a
  reviewer who removed a proposed task must not get it back at install.

The graph is a **lower bound** (calls through ``self`` or an instance, dynamic
dispatch and callbacks give no edge), which is why the planner is still asked
to cover those itself and why the post-wave cascade remains the safety net.
Class targets get findings only: references to a class include annotations
that never need updating, so proposing tasks for them would mostly create
no-op work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from mak.core.types import NodeId, SubTask
from mak.planner.depgraph import DepGraph
from mak.planner.depgraph import referrers as invert_references
from mak.planner.findings import PlanFinding
from mak.scheduler.lock_policy import api_write_targets

CALLER_TASK_PREFIX = "mak.callers."
_CALL_KINDS = frozenset({"function", "method"})
_CHECKED_KINDS = frozenset({"function", "method", "class"})

Referrers = Mapping[NodeId, frozenset[NodeId]]


@dataclass(frozen=True, slots=True)
class MissingCaller:
    """A graph-visible caller of a changing target that no task updates."""

    caller: NodeId
    target: NodeId
    task_id: str


@dataclass(frozen=True, slots=True)
class CallerProposal:
    """The plan with caller-update tasks added, and what was not added."""

    plan: list[SubTask]
    proposed_ids: frozenset[str]
    findings: list[PlanFinding]
    # A covered caller's original ``missing_caller`` message → the message
    # that says which proposed task now updates it.
    covered_messages: dict[str, str] = field(default_factory=dict)

    def annotate(self, findings: list[PlanFinding]) -> list[PlanFinding]:
        """Reword ``missing_caller`` findings whose caller a proposed task covers."""
        return [
            replace(f, message=self.covered_messages[f.message])
            if f.kind == "missing_caller" and f.message in self.covered_messages
            else f
            for f in findings
        ]


def _split(node: NodeId) -> tuple[str, str, str]:
    text = str(node)
    if "::" not in text:
        return text, "", ""
    file_path, kind, name = text.split("::", 2)
    return file_path, kind, name


def _covered(plan: list[SubTask]) -> tuple[set[NodeId], set[str]]:
    """Every targeted node, and every file some task targets as a whole."""
    targets = {node for task in plan for node in task.target_nodes}
    whole = {str(node) for node in targets if "::" not in str(node)}
    return targets, whole


def _is_covered(node: NodeId, targets: set[NodeId], whole: set[str]) -> bool:
    return node in targets or _split(node)[0] in whole


def find_missing_callers(
    plan: list[SubTask],
    graph: DepGraph,
    referrers: Referrers | None = None,
) -> list[MissingCaller]:
    """Return the uncovered callers of every target a task declares changing.

    Only ``changes_api is True`` tasks, and only their ``function``, ``method``
    and ``class`` API targets. A caller is covered when a task targets it or
    its whole file — including the changing task itself.
    """
    refs = referrers if referrers is not None else invert_references(graph)
    targets, whole = _covered(plan)
    missing: list[MissingCaller] = []
    for task in plan:
        if task.changes_api is not True:
            continue
        for target in api_write_targets(task):
            if _split(target)[1] not in _CHECKED_KINDS:
                continue
            for caller in sorted(refs.get(target, frozenset())):
                if not _is_covered(caller, targets, whole):
                    missing.append(MissingCaller(caller, target, task.task_id))
    return missing


def _missing_message(missing: MissingCaller, proposed: str | None = None) -> str:
    if proposed is None:
        return (
            f"'{missing.caller}' references '{missing.target}', whose API "
            f"'{missing.task_id}' changes; no task updates it"
        )
    return (
        f"added: '{proposed}' updates '{missing.caller}', which references "
        f"'{missing.target}', whose API '{missing.task_id}' changes"
    )


def caller_findings(
    plan: list[SubTask], graph: DepGraph, referrers: Referrers | None = None
) -> list[PlanFinding]:
    """Report missing callers and undeclared changes with outside callers."""
    refs = referrers if referrers is not None else invert_references(graph)
    findings = [
        PlanFinding(
            "missing_caller", m.task_id, _missing_message(m), (str(m.caller),)
        )
        for m in find_missing_callers(plan, graph, refs)
    ]
    findings.extend(_undeclared_findings(plan, refs))
    return findings


def _undeclared_findings(plan: list[SubTask], refs: Referrers) -> list[PlanFinding]:
    """One finding per undeclared task whose call targets have outside callers."""
    targets, whole = _covered(plan)
    findings: list[PlanFinding] = []
    for task in plan:
        if task.changes_api is not None:
            continue
        outside = {
            caller
            for target in task.target_nodes
            if _split(target)[1] in _CALL_KINDS
            for caller in refs.get(target, frozenset())
            if not _is_covered(caller, targets, whole)
        }
        if outside:
            count = len(outside)
            findings.append(PlanFinding(
                "undeclared_api_callers", task.task_id,
                f"{count} graph caller{'' if count == 1 else 's'} of these targets "
                f"{'is' if count == 1 else 'are'} outside the plan; declare "
                "changes_api so MAK can check them",
            ))
    return findings


def propose_caller_tasks(
    plan: list[SubTask],
    graph: DepGraph,
    referrers: Referrers | None = None,
    *,
    max_tasks: int,
) -> CallerProposal:
    """Add one caller-update task per caller file, up to ``max_tasks``.

    Each task targets that file's uncovered caller nodes, depends on every
    changing task whose target they reference (so no node is written twice
    unordered), declares ``changes_api: false`` and reads the changed targets as
    context. A task that would break a plan invariant is not added and becomes
    a ``caller_task_refused`` finding; files beyond the cap get one
    ``caller_tasks_capped`` finding.
    """
    missing = [
        m for m in find_missing_callers(plan, graph, referrers)
        if _split(m.target)[1] in _CALL_KINDS
    ]
    by_file: dict[str, list[MissingCaller]] = {}
    for item in missing:
        by_file.setdefault(_split(item.caller)[0], []).append(item)
    augmented = list(plan)
    proposed: list[str] = []
    findings: list[PlanFinding] = []
    covered: dict[str, str] = {}
    capped: list[str] = []
    changers = {task.task_id: task for task in plan}
    for file_path in sorted(by_file):
        if len(proposed) >= max_tasks:
            capped.append(file_path)
            continue
        task_id = _unique_id(len(proposed) + 1, {t.task_id for t in augmented})
        task = _caller_task(task_id, by_file[file_path], changers)
        conflict = granularity_conflict(augmented, task)
        if conflict is not None:
            findings.append(PlanFinding(
                "caller_task_refused", task.depends_on[0],
                f"no caller task for '{file_path}': {conflict}",
                tuple(str(n) for n in task.target_nodes),
            ))
            continue
        augmented.append(task)
        proposed.append(task_id)
        for item in by_file[file_path]:
            covered[_missing_message(item)] = _missing_message(item, task_id)
    if capped:
        findings.append(_capped_finding(capped, max_tasks))
    return CallerProposal(augmented, frozenset(proposed), findings, covered)


def _unique_id(number: int, taken: set[str]) -> str:
    """Return ``mak.callers.<number>``, suffixed on a clash with a planner id."""
    candidate = f"{CALLER_TASK_PREFIX}{number}"
    suffix = 2
    while candidate in taken:
        candidate = f"{CALLER_TASK_PREFIX}{number}_{suffix}"
        suffix += 1
    return candidate


def _caller_task(
    task_id: str, items: list[MissingCaller], changers: Mapping[str, SubTask]
) -> SubTask:
    """Build the caller-update task for one file's missing callers."""
    nodes = sorted({item.caller for item in items})
    targets = sorted({item.target for item in items})
    owners = sorted({item.task_id for item in items})
    owner_of = {item.target: item.task_id for item in items}
    sentences = [
        _update_sentence(target, changers[owner_of[target]]) for target in targets
    ]
    sentences.append("Change only call sites; keep these nodes' own signatures.")
    return SubTask(
        task_id=task_id,
        description=" ".join(sentences),
        target_nodes=nodes,
        context_nodes=targets,
        depends_on=owners,
        agent_type="",
        changes_api=False,
    )


def _update_sentence(target: NodeId, changer: SubTask) -> str:
    symbol = _split(target)[2].split("#", 1)[0]
    contract = changer.contract.get(target)
    if contract:
        return (
            f"Update the references to `{symbol}` (`{target}`) in these nodes to "
            f"match its new signature: `{contract}`."
        )
    return (
        f"Update the references to `{symbol}` (`{target}`) in these nodes to "
        f"match the new signature committed by `{changer.task_id}`."
    )


def _capped_finding(capped: list[str], max_tasks: int) -> PlanFinding:
    count = len(capped)
    return PlanFinding(
        "caller_tasks_capped", "mak.callers",
        f"{count} more caller file{'' if count == 1 else 's'} got no task "
        f"(planner.max_caller_tasks is {max_tasks}); their callers are reported "
        "as missing_caller",
        tuple(capped),
    )


def granularity_conflict(plan: list[SubTask], task: SubTask) -> str | None:
    """Return the ``parse_plan`` invariant ``task`` would break, or None.

    The two plan-wide rules a new task can break: a whole file has exactly one
    writer (**whole-file ownership**), and a file is written either whole or by
    symbol, never both (**one granularity per file**).
    """
    whole: dict[str, str] = {}
    fragments: dict[str, str] = {}
    for other in plan:
        for node in other.target_nodes:
            file_path = _split(node)[0]
            (fragments if "::" in str(node) else whole)[file_path] = other.task_id
    for node in task.target_nodes:
        file_path = _split(node)[0]
        if "::" not in str(node) and file_path in whole:
            return (
                f"whole-file ownership: '{whole[file_path]}' already writes the "
                f"whole file '{file_path}'"
            )
        if "::" not in str(node) and file_path in fragments:
            return (
                f"one granularity per file: '{fragments[file_path]}' writes "
                f"symbols of '{file_path}', which this task would write whole"
            )
        if "::" in str(node) and file_path in whole:
            return (
                f"one granularity per file: '{whole[file_path]}' writes "
                f"'{file_path}' as a whole file"
            )
    return None


def drop_tasks(plan: list[SubTask], task_ids: frozenset[str]) -> list[SubTask]:
    """Remove ``task_ids`` from ``plan`` — safe for MAK-proposed caller tasks.

    Proposed tasks are leaves: nothing the planner wrote depends on them. The
    only edges into one are edges validation added, and those are removed with
    it (the dropped task's callers are reported as ``missing_caller`` again when
    the plan is installed).
    """
    kept = [
        replace(task, depends_on=[d for d in task.depends_on if d not in task_ids])
        for task in plan
        if task.task_id not in task_ids
    ]
    assert not any(d in task_ids for t in kept for d in t.depends_on)
    assert not any(t.task_id in task_ids for t in kept)
    return kept
