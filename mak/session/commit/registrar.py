"""Replay keyed-registrar appends onto the table's current content."""

from __future__ import annotations

from mak.core.logging import EventType
from mak.core.types import LockMode, NodeId
from mak.node_store.store import source_digest
from mak.semantic.read_set import ReadMark
from mak.semantic.registry_merge import MergeKind, plan_merge
from mak.session.commit.verdict import (
    CommitContext,
    Verdict,
    accept,
    defer,
    resend,
)


class RegistrarMerge:
    """Merge a pure keyed append onto whatever the registrar holds now.

    Only targets held INTENT_WRITE are touched: other tasks append to them
    concurrently by design. A pure keyed append is merged onto the current
    table (no lost update) and returned as ``restaged``. Anything else must
    upgrade to an exclusive WRITE, and waits when it cannot — or is sent back
    when the table moved since the agent read it, which an overwrite would undo.
    """

    name = "registrar_merge"

    def check(self, ctx: CommitContext) -> Verdict:
        """Merge every appendable registrar edit; upgrade or wait on the rest."""
        restaged: dict[NodeId, str] = {}
        for node_id in ctx.staged:
            if ctx.granted_mode(node_id) is not LockMode.INTENT_WRITE:
                continue
            fragment = ctx.view.store.get_staged(node_id)
            if fragment is None:
                continue
            mark = ctx.read_mark(node_id)
            current = ctx.view.source(node_id)
            plan = plan_merge(mark.source if mark else None, fragment.source, current)
            if plan.kind is MergeKind.MERGED and plan.source is not None:
                restaged[node_id] = plan.source
                _log_merge(ctx, node_id, plan.appended, mark)
                continue
            verdict = _upgrade(ctx, node_id, plan.reason, restaged)
            if verdict is not None:
                return verdict
        return accept(restaged)


def _log_merge(
    ctx: CommitContext,
    node_id: NodeId,
    appended: tuple[object, ...],
    mark: ReadMark | None,
) -> None:
    """Record a commutative merge with the versions it reconciled."""
    current = ctx.view.committed(node_id)
    ctx.log(
        EventType.REGISTRY_MERGED,
        task_id=ctx.task_id,
        node_id=str(node_id),
        appended=[getattr(e, "text", str(e)) for e in appended],
        read_version=mark.version if mark else None,
        current_version=current.version if current else None,
    )


def _upgrade(
    ctx: CommitContext, node_id: NodeId, why: str, restaged: dict[NodeId, str]
) -> Verdict | None:
    """Take a registrar exclusively for a non-append edit, or say why not.

    Returns None when the edit may proceed under the upgraded lock.
    """
    if not ctx.escalate([(node_id, LockMode.WRITE)]):
        return defer(
            f"'{node_id}' is being appended to concurrently and this edit "
            f"needs it exclusively ({why})",
            restaged=restaged,
        )
    mark = ctx.read_mark(node_id)
    current = ctx.view.committed(node_id)
    current_digest = source_digest(current.source) if current else None
    if mark is not None and mark.digest == current_digest:
        return None
    return resend(
        [f"'{node_id}' changed since it was read; an overwrite would lose entries"],
        note=(
            f"'{node_id}' gained entries from other tasks while you worked, "
            "and your version would have overwritten them. Your bundle now "
            "holds its current content: make your change on top of it."
        ),
        error_kind="stale_read",
        restaged=restaged,
    )
