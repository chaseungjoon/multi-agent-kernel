"""The commit pipeline: run every check in order, then apply or hand off."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from mak.config import SemanticConfig
from mak.conflict_detector.detector import ConflictDetector
from mak.core.logging import EventType
from mak.core.types import NodeId
from mak.session.adjudication import AdjudicatorFence
from mak.session.commit.apply import CommitApplier
from mak.session.commit.checks import (
    ContractsHold,
    LeaseStillHeld,
    PreviewCompiles,
    ProvidersCommitted,
    StructuralConflicts,
)
from mak.session.commit.interface import InterfaceGranted
from mak.session.commit.prospective import ProspectiveSemantics
from mak.session.commit.reads import ReadSetCurrent
from mak.session.commit.registrar import RegistrarMerge
from mak.session.commit.verdict import CommitCheck, CommitContext, Verdict
from mak.session.events import EventLog, timed_phase
from mak.session.failures import record_failure
from mak.session.parking import ParkedCommits
from mak.session.store_view import StoreView
from mak.session.types import LockTableLike
from mak.session.wave import WaveState

# The default order, as data. Cheap, state-only questions come first (is the
# task still waiting on a provider? does its registrar append merge?), then
# staleness, then the edit's own consistency, and the lease re-validation last,
# immediately before the store is advanced. Reordering is a reviewed change:
# tests pin this tuple.
DEFAULT_CHECKS: tuple[type[CommitCheck], ...] = (
    ProvidersCommitted,
    RegistrarMerge,
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
) -> tuple[CommitCheck, ...]:
    """Instantiate :data:`DEFAULT_CHECKS`, in order, for one session."""
    return (
        ProvidersCommitted(),
        RegistrarMerge(),
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
        if not staged:
            return []
        ctx = CommitContext(
            task_id=task_id,
            staged=staged,
            peers=peers or {},
            wave=wave,
            view=self._view,
            lock_table=self._lock_table,
            log=self._log,
        )
        for check in self.checks:
            verdict = check.check(ctx)
            if verdict.restaged:
                self._restage(verdict.restaged)
            if verdict.kind != "accept":
                self._handle(ctx, verdict)
                return []
        return self._applier.apply(wave, task_id, staged)

    def _restage(self, restaged: Mapping[NodeId, str]) -> None:
        """Stage rewritten sources (a merged registrar) in place of the agent's."""
        store = self._view.store
        for node_id, source in restaged.items():
            fragment = store.get_staged(node_id)
            if fragment is not None and fragment.source != source:
                store.put_node(node_id, replace(fragment, source=source))

    def _handle(self, ctx: CommitContext, verdict: Verdict) -> None:
        """Carry out a non-accept verdict: the one place its consequences live."""
        wave, task_id = ctx.wave, ctx.task_id
        if verdict.kind == "defer":
            self._parking.defer(
                wave, task_id, verdict.reason,
                providers=verdict.waiting_on == "providers",
            )
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
                return
        self._rollback(ctx.staged)

    def _rollback(self, staged: list[NodeId]) -> None:
        """Discard the attempt's pending fragments."""
        for node_id in staged:
            self._view.store.rollback_node(node_id)
