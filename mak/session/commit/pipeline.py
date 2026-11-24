"""The commit pipeline: run every check in order, then apply or hand off."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from mak.config import SemanticConfig, SessionConfig
from mak.conflict_detector.detector import ConflictDetector
from mak.core.logging import EventType
from mak.core.types import NodeFragment, NodeId
from mak.session.adjudication import AdjudicatorFence
from mak.session.commit.apply import CommitApplier
from mak.session.commit.checks import (
    ContractsHold,
    LeaseStillHeld,
    PreviewCompiles,
    ProvidersCommitted,
    StructuralConflicts,
)
from mak.session.commit.escalation import LockEscalation
from mak.session.commit.growth import AdditiveSymbols, ImportMerge
from mak.session.commit.interface import InterfaceGranted
from mak.session.commit.prospective import ProspectiveSemantics
from mak.session.commit.reads import ReadSetCurrent
from mak.session.commit.registrar import RegistrarMerge
from mak.session.commit.verdict import (
    CommitCheck,
    CommitContext,
    NewStaging,
    Verdict,
)
from mak.session.events import EventLog, timed_phase
from mak.session.failures import record_failure
from mak.session.parking import ParkedCommits
from mak.session.store_view import StoreView
from mak.session.types import LockTableLike
from mak.session.wave import WaveState

# The default order, as data. Cheap, state-only questions come first (is the
# task still waiting on a provider?), then the write set grows to what the
# reply asked for (escalated nodes, new helpers, merged registrar appends and
# imports) so every later check sees the commit whole; then staleness, then the
# edit's own consistency, and the lease re-validation last, immediately before
# the store is advanced. Reordering is a reviewed change: tests pin this tuple.
DEFAULT_CHECKS: tuple[type[CommitCheck], ...] = (
    ProvidersCommitted,
    LockEscalation,
    AdditiveSymbols,
    RegistrarMerge,
    ImportMerge,
    ReadSetCurrent,
    StructuralConflicts,
    ContractsHold,
    InterfaceGranted,
    PreviewCompiles,
    ProspectiveSemantics,
    LeaseStillHeld,
)


def default_checks(
    *,
    semantic: SemanticConfig,
    detector: ConflictDetector,
    adjudicator: AdjudicatorFence | None = None,
    session: SessionConfig | None = None,
) -> tuple[CommitCheck, ...]:
    """Instantiate :data:`DEFAULT_CHECKS`, in order, for one session."""
    session = session or SessionConfig()
    return (
        ProvidersCommitted(),
        LockEscalation(max_escalations=session.max_escalations),
        AdditiveSymbols(),
        RegistrarMerge(),
        ImportMerge(),
        ReadSetCurrent(
            policy=semantic.stale_read, detector=detector, adjudicator=adjudicator
        ),
        StructuralConflicts(detector),
        ContractsHold(),
        InterfaceGranted(api_locks=semantic.api_locks),
        PreviewCompiles(),
        ProspectiveSemantics(),
        LeaseStillHeld(),
    )


class CommitPipeline:
    """Validate a task's staged fragments with each check, then commit them.

    The pipeline stops at the first verdict that is not ``accept`` and hands it
    to the handler for its kind; after the last check it commits through the
    :class:`~mak.session.commit.apply.CommitApplier`. The store is only
    advanced once every check has accepted, and a failure inside the commit
    transaction reverts it so disk and store never diverge.

    ``checks`` is the plug-in point: a new commit-time check is one more entry
    in the tuple, and needs no change anywhere else.
    """

    def __init__(
        self,
        *,
        checks: Iterable[CommitCheck],
        applier: CommitApplier,
        view: StoreView,
        lock_table: LockTableLike,
        parking: ParkedCommits,
        max_attempts: int,
        log: EventLog,
    ) -> None:
        self.checks: tuple[CommitCheck, ...] = tuple(checks)
        self._applier = applier
        self._view = view
        self._lock_table = lock_table
        self._parking = parking
        self._max_attempts = max_attempts
        self._log = log

    @timed_phase("validate_commit_reconstruct")
    def commit(
        self,
        wave: WaveState,
        task_id: str,
        staged: list[NodeId],
        peers: Mapping[str, str] | None = None,
    ) -> list[NodeId]:
        """Validate, then transactionally commit ``staged``; return what committed.

        ``peers`` are the sources committed earlier in the same batch, so a
        genuine cross-agent conflict is attributed to the later task.
        """
        growth = wave.growth.get(task_id)
        if not staged and (growth is None or growth.empty):
            return []
        ctx = CommitContext(
            task_id=task_id,
            staged=list(staged),
            peers=peers or {},
            wave=wave,
            view=self._view,
            lock_table=self._lock_table,
            log=self._log,
        )
        try:
            return self._run(ctx)
        finally:
            ctx.release_transient()

    def _run(self, ctx: CommitContext) -> list[NodeId]:
        """Run every check, then apply; the body of :meth:`commit`."""
        for check in self.checks:
            verdict = check.check(ctx)
            if verdict.restaged:
                self._restage(verdict.restaged)
            if verdict.extra:
                self._stage_extra(ctx, verdict.extra)
            if verdict.kind == "escalate":
                self._restart(ctx, verdict)
                return []
            if verdict.widen or verdict.widen_context:
                self._widen(ctx, verdict.widen, verdict.widen_context)
            if verdict.kind != "accept":
                self._handle(ctx, verdict)
                return []
        if not ctx.staged:
            return []
        committed = self._applier.apply(ctx.wave, ctx.task_id, ctx.staged)
        if committed:
            for callback in ctx.on_commit:
                callback()
        return committed

    def _restage(self, restaged: Mapping[NodeId, str]) -> None:
        """Stage rewritten sources (a merged registrar) in place of the agent's."""
        store = self._view.store
        for node_id, source in restaged.items():
            fragment = store.get_staged(node_id)
            if fragment is not None and fragment.source != source:
                store.put_node(node_id, replace(fragment, source=source))

    def _stage_extra(
        self, ctx: CommitContext, extra: Mapping[NodeId, NewStaging]
    ) -> None:
        """Stage the nodes a check adds to the commit, in the order it gave."""
        store = self._view.store
        for node_id, new in extra.items():
            fragment = NodeFragment(node_id, new.kind, new.source, 1)
            if new.place:
                store.insert_node(node_id, fragment, after=new.after)
            else:
                store.put_node(node_id, fragment)
            if node_id not in ctx.staged:
                ctx.staged.append(node_id)
                ctx.extras.append(node_id)

    def _widen(
        self,
        ctx: CommitContext,
        targets: tuple[NodeId, ...],
        context: tuple[NodeId, ...],
    ) -> None:
        """Grow the task's targets and context in the plan and its progress."""
        ctx.wave.require_scheduler().widen_task(
            ctx.task_id, targets=targets, context=context
        )
        progress = ctx.progress
        for node_id in targets:
            if node_id not in progress.target_nodes:
                progress.target_nodes.append(node_id)
                progress.optional_nodes.add(node_id)
        ctx.refresh_task()

    def _restart(self, ctx: CommitContext, verdict: Verdict) -> None:
        """Give back every lock and re-queue the task with a wider grant.

        Waiting for the extra nodes while holding the original grant is the
        hold-and-wait condition atomic pre-allocation exists to rule out, so
        the task starts over and its next dispatch takes the whole set at once.
        """
        wave, task_id = ctx.wave, ctx.task_id
        self._rollback(ctx.staged)
        ctx.release_transient()
        progress = ctx.progress
        progress.kernel_note = verdict.retry_note
        progress.error_kind = verdict.error_kind
        wave.granted.pop(task_id, None)
        wave.redispatches += 1
        wave.escalation_restarts += 1
        wave.require_scheduler().requeue_with_targets(
            task_id, targets=verdict.widen, context=verdict.widen_context
        )
        for node_id in verdict.widen:
            if node_id not in progress.target_nodes:
                progress.target_nodes.append(node_id)
                progress.optional_nodes.add(node_id)
        wave.restarted.add(task_id)
        record_failure(wave, task_id, verdict.reason)

    def _handle(self, ctx: CommitContext, verdict: Verdict) -> None:
        """Carry out a non-accept verdict: the one place its consequences live."""
        wave, task_id = ctx.wave, ctx.task_id
        if verdict.kind == "defer":
            self._parking.defer(
                wave, task_id, verdict.reason,
                providers=verdict.waiting_on == "providers",
            )
            # The parked result keeps the agent's own sources; what the kernel
            # added is recomputed when it resumes.
            self._rollback(ctx.extras)
            return
        if verdict.kind == "reject":
            wave.conflict_rejections += 1
            self._log(
                EventType.CONFLICT_DETECTED,
                task_id=task_id,
                reasons=list(verdict.reasons),
            )
            if verdict.reasons:
                record_failure(wave, task_id, verdict.reason)
        elif verdict.kind == "resend":
            ctx.progress.kernel_note = verdict.retry_note
            ctx.progress.error_kind = verdict.error_kind
            record_failure(wave, task_id, verdict.reason)
        elif verdict.kind == "fail":
            record_failure(wave, task_id, verdict.reason)
            ctx.progress.attempts = self._max_attempts
            if task_id in wave.deferring:
                self._rollback(ctx.extras)
                return
        self._rollback(ctx.staged)

    def _rollback(self, staged: list[NodeId]) -> None:
        """Discard the attempt's pending fragments."""
        for node_id in staged:
            self._view.store.rollback_node(node_id)
