"""The commit checks that need no state of their own beyond the context."""

from __future__ import annotations

from mak.conflict_detector.detector import ConflictDetector, EditRound
from mak.core.exceptions import ContractError
from mak.core.logging import EventType
from mak.core.types import NodeId
from mak.planner.contracts import contract_stub, implementation_mismatch
from mak.semantic.contracts import visible_contracts
from mak.session.commit.verdict import (
    ACCEPT,
    CommitContext,
    Verdict,
    defer,
    fail,
    reject,
    resend,
)
from mak.session.store_view import StoreView, file_of, is_header_id


class ProvidersCommitted:
    """Hold a contract-dispatched task's commit until its providers commit.

    A task dispatched ahead of a soft dependency was built against the
    provider's *contract*; its code may call what does not exist yet, so it
    waits until the provider commits — and fails with it if it fails.
    """

    name = "providers_committed"

    def check(self, ctx: CommitContext) -> Verdict:
        """Accept once every soft provider committed; defer or fail otherwise."""
        dag = ctx.wave.require_scheduler().dag
        waiting = sorted(
            dep
            for dep in dag.soft_dependencies(ctx.task_id)
            if not dag.is_complete(dep)
        )
        failed = [dep for dep in waiting if dep in ctx.wave.failed]
        if failed:
            return fail(
                f"its contract provider(s) {', '.join(failed)} failed, so the "
                "interface it was built against was never implemented"
            )
        if waiting:
            return defer(
                f"built against the declared contract of {', '.join(waiting)}; "
                "waits for them to commit",
                providers=True,
            )
        return ACCEPT


class StructuralConflicts:
    """Run the conflict detector over this edit and the batch's earlier commits.

    ``definitions`` spans every staged source in the batch (this task's plus
    the peers already committed), so a signature change anywhere is the
    authority for this task's call sites — the cross-agent signature check.
    ``symbol_edits`` / ``header_edits`` are scoped to the *files this task
    touches*: name collisions and import conflicts are file-local, so feeding
    unrelated files would only invent false positives.
    """

    name = "structural_conflicts"

    def __init__(self, detector: ConflictDetector) -> None:
        self._detector = detector

    def check(self, ctx: CommitContext) -> Verdict:
        """Reject when the detector finds a conflict in the edit round."""
        report = self._detector.detect(build_edit_round(ctx))
        return ACCEPT if report.ok else reject(report.reasons)


def build_edit_round(ctx: CommitContext) -> EditRound:
    """Assemble an EditRound from the staged fragments plus this batch's peers."""
    own = {str(node_id): source for node_id, source in ctx.staged_sources().items()}
    own_files = {file_of(k) for k in own}
    definitions = {**_contract_definitions(ctx, own), **ctx.peers, **own}
    same_file = {k: v for k, v in definitions.items() if file_of(k) in own_files}
    headers = {k: v for k, v in same_file.items() if is_header_id(k)}
    previous = {
        k: source
        for k in own
        if (source := ctx.view.source(NodeId(k))) is not None
    }
    # Each staged source is both a definition authority and a caller, so the
    # detector validates this task's new calls against every new signature.
    return EditRound(
        definitions=definitions,
        callers=own,
        header_edits=headers,
        symbol_edits=same_file,
        registry_edits=own,
        previous=previous,
    )


def _contract_definitions(ctx: CommitContext, own: dict[str, str]) -> dict[str, str]:
    """Contract stubs a task's calls are checked against at its commit."""
    plan = ctx.wave.require_scheduler().dag.tasks
    task = ctx.task
    stubs: dict[str, str] = {}
    for node_id, text in visible_contracts(task, plan).items():
        if node_id in task.contract or str(node_id) in own:
            continue
        try:
            stubs[str(node_id)] = contract_stub(text)
        except ContractError:
            continue  # a contract the planner never validated is no authority
    return stubs


class ContractsHold:
    """The provider side of a contract: a contracted node must match it."""

    name = "contracts_hold"

    def check(self, ctx: CommitContext) -> Verdict:
        """Send the attempt back when a contracted node drifts from its contract."""
        for node_id, text in ctx.task.contract.items():
            fragment = ctx.view.store.get_staged(node_id)
            if fragment is None:
                continue
            reason = implementation_mismatch(text, fragment.source)
            if reason is None:
                continue
            ctx.log(
                EventType.CONTRACT_VIOLATION, task_id=ctx.task_id,
                node_id=str(node_id), reason=reason,
            )
            return resend(
                [f"'{node_id}' does not match its declared contract: {reason}"],
                note=(
                    f"'{node_id}' must implement its declared contract exactly — "
                    f"other tasks are being built against it: {reason}. Keep the "
                    "declared name, parameters (names, order, annotations, "
                    "defaults) and return annotation."
                ),
                error_kind="contract_violation",
            )
        return ACCEPT


class PreviewCompiles:
    """Every file the edit touches must still be valid Python once assembled."""

    name = "preview_compiles"

    def check(self, ctx: CommitContext) -> Verdict:
        """Reject an edit whose assembled files would not compile."""
        if preview_is_valid(ctx.view, ctx.staged):
            return ACCEPT
        return reject(["reconstruction would produce invalid Python"])


def preview_is_valid(view: StoreView, staged: list[NodeId]) -> bool:
    """Assemble each affected file with staged versions and check it parses."""
    staged_set = set(staged)
    for file_path in sorted({file_of(str(n)) for n in staged}):
        try:
            compile(view.preview(file_path, staged_set), "<mak-preview>", "exec")
        except SyntaxError:
            return False
    return True


class LeaseStillHeld:
    """The task must still hold every lease it is about to commit through.

    A lease may have expired during a long agent call and the node been
    reclaimed by another holder; the store is never advanced through a lock
    the task no longer owns.
    """

    name = "lease_still_held"

    def check(self, ctx: CommitContext) -> Verdict:
        """Reject when any granted lease has lapsed."""
        held = ctx.lock_table.holds_all(
            [(node_id, ctx.granted_mode(node_id)) for node_id in ctx.staged],
            ctx.task_id,
        )
        if held:
            return ACCEPT
        return reject(["write lock lost before commit (lease expired)"])
