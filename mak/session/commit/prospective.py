"""Judge a candidate edit against the whole repository before it is committed."""

from __future__ import annotations

from mak.conflict_detector.cycle_check import check_new_cycles
from mak.core.types import NodeId
from mak.semantic.sources import StoreSources
from mak.session.commit.verdict import ACCEPT, CommitContext, Verdict, reject
from mak.session.repair import post_wave_checks, unresolved_obligations
from mak.session.store_view import file_of


class ProspectiveSemantics:
    """Reject a candidate that introduces a defect or leaves its obligation open.

    The conflict detector judges the fragments in one edit round. Cross-module
    truth needs the complete repository, including untouched providers and
    callers: build that view with the staged files substituted, compare it with
    the committed baseline, and do so before the store transaction or audit
    commit can make a hallucination durable.
    """

    name = "prospective_semantics"

    def check(self, ctx: CommitContext) -> Verdict:
        """Reject with every defect introduced and every obligation still open."""
        reasons = _prospective_reasons(ctx)
        return reject(reasons) if reasons else ACCEPT


def _prospective_reasons(ctx: CommitContext) -> list[str]:
    """Why the repository would be worse with this edit, deduplicated."""
    touched = sorted({file_of(str(node)) for node in ctx.staged})
    overrides: dict[str, str | None] = {
        file_path: ctx.view.preview(file_path, set(ctx.staged))
        for file_path in touched
    }
    before = StoreSources(ctx.view.store)
    after = before.with_overrides(overrides)
    scope = _prospective_scope(ctx)

    baseline = post_wave_checks(before, scope)
    candidate = post_wave_checks(after, scope)
    baseline_keys = {defect.exact_key for defect in baseline}
    introduced = [
        defect for defect in candidate if defect.exact_key not in baseline_keys
    ]
    introduced.extend(check_new_cycles(before, after, scope))

    task = ctx.task
    if not task.repair_obligations:
        other_targets = {
            file_of(str(node))
            for other_id, other in ctx.wave.require_scheduler().dag.tasks.items()
            if other_id != ctx.task_id
            for node in other.target_nodes
        }
        introduced = [
            defect
            for defect in introduced
            if defect.file in touched
            if not {defect.file, defect.defining_file} & other_targets
        ]
    unresolved = unresolved_obligations(after, task, scope, candidate)

    reasons = [
        f"prospective repository validation rejected the edit: {defect.detail}"
        for defect in introduced
    ]
    reasons.extend(
        (
            "repair obligation remains unresolved after the proposed edit: "
            f"{obligation.detail}"
        )
        for obligation in unresolved
    )
    return list(dict.fromkeys(reasons))


def _prospective_scope(ctx: CommitContext) -> frozenset[str]:
    """Files whose cross-module agreement a staged edit can change."""
    touched = {file_of(str(node)) for node in ctx.staged}
    scope = set(touched)
    for obligation in ctx.task.repair_obligations:
        scope.add(obligation.file)
        scope.add(obligation.defining_file)

    # A provider edit can break untouched callers. Expand whole-file targets
    # to their committed symbol nodes before walking reverse references.
    providers: set[NodeId] = set(ctx.staged)
    for file_path in touched:
        providers.update(ctx.view.store.list_nodes(file_path))
    graph = ctx.wave.graph
    if graph is not None:
        for caller, references in graph.references.items():
            if references & providers:
                scope.add(file_of(str(caller)))
    return frozenset(scope)
