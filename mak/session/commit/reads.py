"""Validate a task's read set: nothing it was shown may have changed under it."""

from __future__ import annotations

from collections.abc import Callable

from mak.conflict_detector.cross_module_check import check_cross_module_api
from mak.conflict_detector.detector import ConflictDetector, EditRound
from mak.core.exceptions import NodeStoreError
from mak.core.logging import EventType
from mak.core.types import LockMode, NodeId
from mak.node_store.store import source_digest
from mak.planner.contracts import implementation_mismatch
from mak.semantic.contracts import visible_contracts
from mak.semantic.read_set import ReadMark
from mak.semantic.sources import StoreSources
from mak.semantic.stale import (
    NodeDecision,
    StaleDecision,
    StaleRead,
    classify,
    decide,
    retry_note,
)
from mak.semantic.stale import Verdict as StaleVerdict
from mak.semantic.symbols import symbol_source
from mak.session.adjudication import AdjudicatorFence
from mak.session.commit.verdict import (
    ACCEPT,
    CommitContext,
    Verdict,
    reject,
    resend,
)
from mak.session.store_view import file_of


class ReadSetCurrent:
    """Backward validation from optimistic concurrency control.

    Every node the task's bundle carried is compared, by digest, with what is
    committed *now*. Nothing changed → the task saw a consistent snapshot.
    Something changed → each stale node is classified and the
    ``semantic.stale_read`` policy decides; every one of them is logged with
    its verdict.
    """

    name = "read_set_current"

    def __init__(
        self,
        *,
        policy: str,
        detector: ConflictDetector,
        adjudicator: AdjudicatorFence | None = None,
    ) -> None:
        self._policy = policy
        self._detector = detector
        self._adjudicator = adjudicator

    def check(self, ctx: CommitContext) -> Verdict:
        """Accept a current read set; reject or re-dispatch a stale one."""
        stale = _stale_reads_of(ctx)
        if not stale:
            return ACCEPT
        ctx.wave.stale_reads += len(stale)
        covered = [s for s in stale if _contract_covered(ctx, s)]
        rest = [s for s in stale if s not in covered]
        adjudicated: set[NodeId] = set()
        decision = decide(
            self._policy,
            rest,
            recheck=lambda nodes: self._recheck_against_current(ctx, nodes),
            adjudicate=self._bind_adjudicator(ctx, adjudicated),
        )
        decision = StaleDecision(
            decision.verdict,
            (
                *(
                    NodeDecision(
                        s, StaleVerdict.ACCEPT,
                        "matches the declared contract the task was built against",
                    )
                    for s in covered
                ),
                *decision.nodes,
            ),
        )
        self._log_decision(ctx, decision, adjudicated)
        if decision.verdict is StaleVerdict.ACCEPT:
            return ACCEPT
        reasons = [
            f"stale read of '{d.stale.node_id}' ({d.stale.kind}): {d.reason}"
            for d in decision.nodes
            if d.verdict is not StaleVerdict.ACCEPT
        ]
        if decision.verdict is StaleVerdict.REJECT:
            return reject(reasons)
        ctx.wave.stale_redispatches += 1
        return resend(reasons, note=retry_note(decision), error_kind="stale_read")

    def _bind_adjudicator(
        self, ctx: CommitContext, accepted: set[NodeId]
    ) -> Callable[[StaleRead], bool | None] | None:
        """Bind the optional adjudicator to this commit's staged code."""
        if self._adjudicator is None:
            return None
        return self._adjudicator.bind(ctx.wave, ctx.staged_sources(), accepted)

    def _recheck_against_current(
        self, ctx: CommitContext, stale: list[StaleRead]
    ) -> list[str]:
        """Re-run the static checks for the staged code against the *current* code.

        The signature check takes the changed nodes' current sources as the
        definition authority; the cross-module checks judge the task's files as
        they would be committed, against the store as it stands.
        """
        own = {str(k): v for k, v in ctx.staged_sources().items()}
        definitions = {
            str(s.node_id): s.current_source
            for s in stale
            if s.current_source is not None
        }
        reasons: list[str] = []
        if definitions and own:
            report = self._detector.detect(
                EditRound(definitions={**definitions, **own}, callers=own)
            )
            reasons.extend(report.reasons)
        reasons.extend(_prospective_defects(ctx))
        return reasons

    def _log_decision(
        self,
        ctx: CommitContext,
        decision: StaleDecision,
        adjudicated: set[NodeId],
    ) -> None:
        """Log one ``STALE_READ`` event per stale node, each with its verdict.

        A verdict the LLM adjudicator reached carries ``nondeterministic: true``.
        """
        attempt = ctx.progress.attempts
        for node in decision.nodes:
            fenced = (
                {"nondeterministic": True}
                if node.stale.node_id in adjudicated
                and node.verdict is StaleVerdict.ACCEPT
                else {}
            )
            ctx.log(
                EventType.STALE_READ,
                task_id=ctx.task_id,
                attempt=attempt,
                node_id=str(node.stale.node_id),
                layer=node.stale.mark.layer,
                read_version=node.stale.mark.version,
                current_version=node.stale.current_version,
                change=str(node.stale.kind),
                referenced=node.stale.referenced,
                verdict=str(node.verdict),
                commit_verdict=str(decision.verdict),
                policy=self._policy,
                reason=node.reason,
                **fenced,
            )


def _stale_reads_of(ctx: CommitContext) -> list[StaleRead]:
    """Every node in the task's read set whose committed content moved."""
    read_set = ctx.wave.read_sets.get(ctx.task_id)
    if not read_set:
        return []
    exempt = _stale_exempt(ctx)
    own = ctx.staged_sources()
    stale: list[StaleRead] = []
    for node_id, mark in read_set.items():
        if node_id in exempt:
            continue
        version, source, digest = _current_for_mark(ctx, mark)
        if digest == mark.digest:
            continue
        stale.append(classify(mark, version, source, own))
    return stale


def _stale_exempt(ctx: CommitContext) -> set[NodeId]:
    """Read-set nodes whose staleness is already settled for this task.

    Its own WRITE-held targets cannot have changed — nobody else can commit
    them. Its INTENT_WRITE-held targets are keyed registrars that other
    tasks append to concurrently *by design*; the registrar merge has already
    replayed this task's append onto whatever they hold now (or sent the
    attempt back), so a newer version there is the expected state, not a
    stale read.
    """
    granted = ctx.wave.granted.get(ctx.task_id, {})
    targets = set(ctx.progress.target_nodes)
    return {
        node_id
        for node_id, mode in granted.items()
        if node_id in targets and mode in (LockMode.WRITE, LockMode.INTENT_WRITE)
    }


def _current_for_mark(
    ctx: CommitContext, mark: ReadMark
) -> tuple[int | None, str | None, str | None]:
    """Return ``(version, source, digest)`` of what a read-set node is *now*.

    A fragment superseded by a whole-file commit is not gone: its symbol is
    looked up in the whole-file node, so a sibling task's whole-file rewrite
    is compared symbol-for-symbol rather than reported as a deletion.
    """
    fragment = ctx.view.committed(mark.node_id)
    if fragment is not None:
        return fragment.version, fragment.source, source_digest(fragment.source)
    parts = str(mark.node_id).split("::")
    if len(parts) >= 3:
        whole = ctx.view.committed(NodeId(parts[0]))
        if whole is not None:
            qualname = parts[2].split("#", 1)[0]
            extracted = symbol_source(whole.source, qualname)
            if extracted is not None:
                return whole.version, extracted, source_digest(extracted)
    return None, None, None


def _contract_covered(ctx: CommitContext, stale: StaleRead) -> bool:
    """Whether a stale node is exactly the contract the task was built against."""
    text = visible_contracts(ctx.task, ctx.wave.require_scheduler().dag.tasks).get(
        stale.node_id
    )
    if text is None or stale.current_source is None:
        return False
    return implementation_mismatch(text, stale.current_source) is None


def _prospective_defects(ctx: CommitContext) -> list[str]:
    """Cross-module defects the task's files would have if committed now."""
    staged_set = set(ctx.staged)
    files = sorted({file_of(str(n)) for n in ctx.staged})
    overrides: dict[str, str | None] = {}
    for file_path in files:
        try:
            overrides[file_path] = ctx.view.preview(file_path, staged_set)
        except (SyntaxError, NodeStoreError):
            continue
    view = StoreSources(ctx.view.store).with_overrides(overrides)
    return [
        defect.detail for defect in check_cross_module_api(view, frozenset(files))
    ]
