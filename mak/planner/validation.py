"""Deterministic validation of a planner-produced DAG against real code structure.

An LLM plan's ``depends_on`` edges and node ids are guesses. MAK owns the AST, so
:func:`validate_plan` cross-checks a plan against the static dependency graph
(:mod:`mak.planner.depgraph`) and the node inventory, then **augments and corrects**
it deterministically:

- **Missing dependency edges** are *added* — if task A rewrites a node that
  references a node task B rewrites, A should depend on B (acyclic-safe).
- **Hallucinated node ids** are *corrected* on a single confident fuzzy match, or
  flagged with suggestions when uncertain; a genuinely new (unmatched) target is
  left alone (new symbols/files are legitimate).
- **Spurious edges** are *flagged only, never removed* — the LLM may know a
  semantic ordering the AST cannot see.
- **Unknown context nodes** are *dropped* — soft references must resolve to a real
  node or be removed — **unless another task in the same plan creates them**, in
  which case they are kept and the reader is ordered after the creator.

The context exception exists because dropping was starving greenfield work. In a
wave that creates new files, a task's context ids name modules *sibling tasks will
write*, so they are absent from the current inventory by construction; deleting
them left the bundle with nothing at all and the agent guessing at APIs. Note the
asymmetry the grounding policy already encoded: a *target* that is genuinely new is
kept as legitimate, while a *context* node in the same position was deleted.

The original objection — "keeping a phantom would read-lock a nonexistent node" —
does not hold. ``Scheduler._lock_requests`` appends ``(node_id, READ)`` for every
context node and the lock table never consults the node store, so locking an id
with no committed fragment is legal; and with the ordering edge added below, the
node has been committed by the time the reader dispatches.

Every change is also reported as a :class:`PlanFinding` so the human-in-the-loop
reviewer sees exactly what validation did and can override it via the edit flow.
Originals are never mutated (``SubTask`` is frozen; corrections use ``replace``).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from mak.core.types import NodeId, SubTask
from mak.planner.depgraph import DepGraph

_STRONG_RATIO = 0.9
_CLOSE_CUTOFF = 0.8


@dataclass(frozen=True, slots=True)
class PlanFinding:
    """One deterministic observation about a plan, for review and logging."""

    # missing_dep | spurious_dep | unknown_node | corrected_node | context_dropped
    kind: str
    task_id: str
    message: str
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """A corrected copy of the plan plus the findings that explain the changes."""

    plan: list[SubTask]
    findings: list[PlanFinding]


def _split_id(node_id: str) -> tuple[str, str, str]:
    """Return ``(file, kind, name)``; kind/name are ``""`` for a whole-file id."""
    if "::" not in node_id:
        return node_id, "", ""
    file_path, kind, name = node_id.split("::", 2)
    return file_path, kind, name


def _norm(name: str) -> str:
    """Case/underscore-insensitive form of a symbol name for fuzzy matching."""
    return name.split("#", 1)[0].replace("_", "").lower()


def _match_candidates(
    bad_id: str, inventory: list[NodeId]
) -> tuple[str | None, tuple[str, ...]]:
    """Return ``(auto_correction, suggestions)`` for an id absent from the inventory.

    Tiers (a)-(c) are exact-structure matches within the same file; the first tier
    to yield candidates decides. A single candidate auto-corrects; several become
    suggestions. Only if all three are empty does a fuzzy (d) match apply, and only
    when exactly one candidate clears the strong ratio.
    """
    bad_file, bad_kind, bad_name = _split_id(bad_id)
    for tier in (_tier_wrong_kind, _tier_normalized_name, _tier_missing_class):
        cands = tier(bad_file, bad_kind, bad_name, inventory)
        if cands:
            return (cands[0], ()) if len(cands) == 1 else (None, tuple(sorted(cands)))
    close = difflib.get_close_matches(
        bad_id, list(inventory), n=3, cutoff=_CLOSE_CUTOFF
    )
    strong = [
        c for c in close
        if SequenceMatcher(None, bad_id, c).ratio() >= _STRONG_RATIO
    ]
    if len(strong) == 1:
        return strong[0], ()
    return None, tuple(close)


def _tier_wrong_kind(
    file: str, kind: str, name: str, inventory: list[NodeId]
) -> list[str]:
    """Match a same-file, same-name node whose ``kind`` segment differs."""
    if not kind:
        return []
    return [
        i for i in inventory
        if (parts := _split_id(i))[0] == file and parts[2] == name and parts[1] != kind
    ]


def _tier_normalized_name(
    file: str, kind: str, name: str, inventory: list[NodeId]
) -> list[str]:
    """Match a same-file node whose short name matches modulo case/underscores."""
    if not name:
        return []
    target = _norm(name.rsplit(".", 1)[-1])
    return [
        i for i in inventory
        if (parts := _split_id(i))[0] == file
        and parts[2]
        and _norm(parts[2].rsplit(".", 1)[-1]) == target
    ]


def _tier_missing_class(
    file: str, kind: str, name: str, inventory: list[NodeId]
) -> list[str]:
    """Match a bare name that is really a method missing its ``Class.`` prefix."""
    if not name or "." in name:
        return []
    return [
        i for i in inventory
        if (parts := _split_id(i))[0] == file
        and parts[1] == "method"
        and parts[2].rsplit(".", 1)[-1] == name
    ]


def _ground_ids(
    ids: list[NodeId],
    known: set[NodeId],
    inventory: list[NodeId],
    task_id: str,
    *,
    is_context: bool,
) -> tuple[list[NodeId], list[PlanFinding]]:
    """Ground one id list; correct/flag/drop per the target vs context policy.

    ``known`` is the set of ids that count as resolved — the inventory for targets,
    the inventory *plus every task's targets* for context. ``inventory`` stays the
    real one either way: it is the fuzzy-match candidate list, and correcting an id
    toward a node that does not exist yet would invent one nobody declared.
    """
    kept: list[NodeId] = []
    findings: list[PlanFinding] = []
    for node_id in ids:
        if node_id in known:
            kept.append(node_id)
            continue
        auto, suggestions = _match_candidates(str(node_id), inventory)
        if auto is not None:
            kept.append(NodeId(auto))
            findings.append(PlanFinding(
                "corrected_node", task_id,
                f"corrected '{node_id}' -> '{auto}'", (auto,),
            ))
        elif is_context:
            findings.append(PlanFinding(
                "context_dropped", task_id,
                f"dropped unknown context node '{node_id}'", suggestions,
            ))
        elif suggestions:
            kept.append(node_id)
            findings.append(PlanFinding(
                "unknown_node", task_id,
                f"target '{node_id}' is not in the inventory", suggestions,
            ))
        else:
            kept.append(node_id)  # genuinely new symbol/file — legitimate, silent
    return kept, findings


def _ground_plan(
    plan: list[SubTask], inventory: list[NodeId]
) -> tuple[list[SubTask], list[PlanFinding]]:
    """Ground every task's targets, then its context against targets *and* inventory.

    Two passes rather than one, because grounding context requires knowing what the
    whole plan will create. Both the planner's original target ids and their
    corrected forms count as creations: a correction can be reverted later by
    ``_guard_whole_file``, and context naming either form refers to the same
    forthcoming node.
    """
    inv = set(inventory)
    grounded_targets: list[list[NodeId]] = []
    findings: list[PlanFinding] = []
    for task in plan:
        targets, target_findings = _ground_ids(
            task.target_nodes, inv, inventory, task.task_id, is_context=False
        )
        grounded_targets.append(targets)
        findings.extend(target_findings)

    known = set(inv)
    for task, targets in zip(plan, grounded_targets, strict=True):
        known.update(task.target_nodes)
        known.update(targets)

    grounded: list[SubTask] = []
    for task, targets in zip(plan, grounded_targets, strict=True):
        context, context_findings = _ground_ids(
            task.context_nodes, known, inventory, task.task_id, is_context=True
        )
        findings.extend(context_findings)
        grounded.append(replace(task, target_nodes=targets, context_nodes=context))
    return grounded, findings


def _whole_file_offenders(plan: list[SubTask]) -> set[str]:
    """Return bare whole-file paths ``parse_plan`` would reject.

    A file is an offender when two tasks both own it whole, or it is targeted at
    both whole-file and fragment granularity — the checks that guard corrections.
    """
    whole_owners: dict[str, int] = {}
    fragment_files: set[str] = set()
    for task in plan:
        for node in dict.fromkeys(str(n) for n in task.target_nodes):
            if "::" in node:
                fragment_files.add(node.split("::", 1)[0])
            else:
                whole_owners[node] = whole_owners.get(node, 0) + 1
    offenders = {path for path, count in whole_owners.items() if count > 1}
    offenders |= set(whole_owners) & fragment_files
    return offenders


def _guard_whole_file(
    plan: list[SubTask], findings: list[PlanFinding]
) -> tuple[list[SubTask], list[PlanFinding]]:
    """Revert any target correction that created a whole-file-owner conflict."""
    offenders = _whole_file_offenders(plan)
    if not offenders:
        return plan, findings
    corrected = {f.suggestions[0]: f for f in findings if f.kind == "corrected_node"}
    reverted: list[PlanFinding] = []
    tasks = list(plan)
    for i, task in enumerate(tasks):
        new_targets = []
        for node in task.target_nodes:
            if str(node) in offenders and str(node) in corrected:
                finding = corrected[str(node)]
                original = finding.message.split("'")[1]  # "corrected '<orig>' -> ..."
                new_targets.append(NodeId(original))
                reverted.append(finding)
            else:
                new_targets.append(node)
        tasks[i] = replace(task, target_nodes=new_targets)
    kept = [f for f in findings if f not in reverted]
    for finding in reverted:
        kept.append(PlanFinding(
            "unknown_node", finding.task_id,
            f"target correction to '{finding.suggestions[0]}' would duplicate a "
            "whole-file owner; left unchanged",
            finding.suggestions,
        ))
    return tasks, kept


def _reachable(deps: dict[str, set[str]], start: str) -> set[str]:
    """All tasks transitively depended on by ``start``."""
    seen: set[str] = set()
    stack = list(deps.get(start, ()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(deps.get(node, ()))
    return seen


def _writers_of(plan: list[SubTask]) -> dict[NodeId, set[str]]:
    """Map each targeted node to the ids of the tasks that will write it."""
    writers: dict[NodeId, set[str]] = {}
    for task in plan:
        for node in task.target_nodes:
            writers.setdefault(node, set()).add(task.task_id)
    return writers


def _try_add_edge(
    deps: dict[str, set[str]],
    mutual_seen: set[frozenset[str]],
    task_id: str,
    writer: str,
    *,
    reason: str,
    mutual_reason: str,
) -> PlanFinding | None:
    """Add ``writer -> task_id`` when it is new and acyclic; report what happened.

    Returns ``None`` when nothing needs saying: the edge is already implied
    transitively, or the pair's mutual conflict has been reported once already.
    Mutates ``deps`` and ``mutual_seen`` in place — both are the caller's
    working state for a single augmentation pass.
    """
    if writer == task_id or writer in _reachable(deps, task_id):
        return None
    if task_id in _reachable(deps, writer):
        pair = frozenset({task_id, writer})
        if pair in mutual_seen:
            return None
        mutual_seen.add(pair)
        return PlanFinding("missing_dep", task_id, mutual_reason)
    deps[task_id].add(writer)
    return PlanFinding("missing_dep", task_id, reason)


def _add_missing_edges(
    plan: list[SubTask], graph: DepGraph
) -> tuple[list[SubTask], list[PlanFinding]]:
    """Add dependency edges grounded in real references; flag unresolvable mutuals."""
    writers = _writers_of(plan)
    deps: dict[str, set[str]] = {t.task_id: set(t.depends_on) for t in plan}
    findings: list[PlanFinding] = []
    mutual_seen: set[frozenset[str]] = set()
    for task in plan:
        for target in task.target_nodes:
            for ref in sorted(graph.references.get(target, frozenset())):
                for writer in sorted(writers.get(ref, set())):
                    finding = _try_add_edge(
                        deps, mutual_seen, task.task_id, writer,
                        reason=(
                            f"added: '{writer}' -> '{task.task_id}' "
                            f"(rewrites node referenced via {ref})"
                        ),
                        mutual_reason=(
                            f"'{task.task_id}' and '{writer}' reference each "
                            "other — mutual dependency, consider merging or "
                            "ordering them manually"
                        ),
                    )
                    if finding is not None:
                        findings.append(finding)
    augmented = [replace(t, depends_on=sorted(deps[t.task_id])) for t in plan]
    return augmented, findings


def _add_forward_context_edges(
    plan: list[SubTask], inventory: list[NodeId]
) -> tuple[list[SubTask], list[PlanFinding]]:
    """Order a task after whichever sibling task creates the context it reads.

    A context node survives grounding without being in the inventory only because
    another task in this plan targets it — so it does not exist yet. Without an
    edge the reader can dispatch first and arrive with nothing, which is the exact
    starvation keeping the context node was meant to prevent. Ids that *are* in the
    inventory need no edge: they are readable at any point in the wave.

    ``_add_missing_edges`` cannot cover this — it works off the ``DepGraph``, which
    is built from committed code and therefore cannot see a file no one has written.
    """
    inv = set(inventory)
    writers = _writers_of(plan)
    deps: dict[str, set[str]] = {t.task_id: set(t.depends_on) for t in plan}
    findings: list[PlanFinding] = []
    mutual_seen: set[frozenset[str]] = set()
    for task in plan:
        for node in task.context_nodes:
            if node in inv:
                continue
            for writer in sorted(writers.get(node, set())):
                finding = _try_add_edge(
                    deps, mutual_seen, task.task_id, writer,
                    reason=(
                        f"added: '{writer}' -> '{task.task_id}' (creates context "
                        f"node '{node}', which does not exist yet)"
                    ),
                    mutual_reason=(
                        f"'{task.task_id}' reads context '{node}' that '{writer}' "
                        f"creates, but '{writer}' already depends on "
                        f"'{task.task_id}' — order them manually"
                    ),
                )
                if finding is not None:
                    findings.append(finding)
    augmented = [replace(t, depends_on=sorted(deps[t.task_id])) for t in plan]
    return augmented, findings


def _has_graph_info(task: SubTask, graph: DepGraph) -> bool:
    return any(node in graph.references for node in task.target_nodes)


def _references_between(a: SubTask, b: SubTask, graph: DepGraph) -> bool:
    """Whether either task's nodes reference the other's targets (either direction)."""
    a_side = set(a.target_nodes) | set(a.context_nodes)
    b_targets = set(b.target_nodes)
    for node in a_side:
        if graph.references.get(node, frozenset()) & b_targets:
            return True
    b_side = set(b.target_nodes)
    a_targets = set(a.target_nodes) | set(a.context_nodes)
    for node in b_side:
        if graph.references.get(node, frozenset()) & a_targets:
            return True
    return False


def _flag_spurious(
    original: list[SubTask], corrected: list[SubTask], graph: DepGraph
) -> list[PlanFinding]:
    """Flag declared edges with no code reference in either direction (never remove)."""
    by_id = {t.task_id: t for t in corrected}
    findings: list[PlanFinding] = []
    for task in original:
        a = by_id.get(task.task_id)
        if a is None:
            continue
        for dep in task.depends_on:
            b = by_id.get(dep)
            if b is None:
                continue
            if not (_has_graph_info(a, graph) and _has_graph_info(b, graph)):
                continue
            if not _references_between(a, b, graph):
                findings.append(PlanFinding(
                    "spurious_dep", task.task_id,
                    f"declared dependency '{dep}' -> '{task.task_id}' has no code "
                    "reference between them (kept; may be a semantic ordering)",
                ))
    return findings


def validate_plan(
    plan: list[SubTask], graph: DepGraph, inventory: list[NodeId]
) -> ValidationResult:
    """Validate and augment ``plan`` against the code graph and node inventory."""
    grounded, findings = _ground_plan(plan, inventory)
    grounded, findings = _guard_whole_file(grounded, findings)
    augmented, edge_findings = _add_missing_edges(grounded, graph)
    findings.extend(edge_findings)
    augmented, forward_findings = _add_forward_context_edges(augmented, inventory)
    findings.extend(forward_findings)
    findings.extend(_flag_spurious(plan, augmented, graph))
    return ValidationResult(plan=augmented, findings=findings)
