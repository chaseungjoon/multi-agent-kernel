"""Grow a task's grant at commit, or restart it — never hold and wait.

A reply that wrote an existing node outside its grant, or declared one in
``needs_nodes``, is asking for more locks than the scheduler gave it. The
scheduler acquires each task's set atomically up front precisely so no task
waits while holding locks; waiting here for the extra nodes would reintroduce
that hold-and-wait. So the extra locks are tried once, without blocking:

- **free** — they join the grant. When the agent already saw every escalated
  node at its current source, its proposal is staged and the commit goes on;
  otherwise it is sent back with the node in its bundle.
- **busy** — every lock the task holds is given back and the task re-queued
  with the nodes added to its targets, so its next dispatch takes the whole
  set at once, like any other task.

Whole-file and class-shell nodes cover what other tasks write; they are never
taken mid-flight, only through a restart.
"""

from __future__ import annotations

from dataclasses import replace

from mak.core.logging import EventType
from mak.core.types import LockMode, NodeId
from mak.node_store.store import source_digest
from mak.scheduler.lock_policy import lock_requests
from mak.session.commit.verdict import (
    ACCEPT,
    CommitContext,
    NewStaging,
    Verdict,
    accept,
    escalate,
    resend,
)
from mak.session.types import Escalation

# How much of the previous reply a restart's note carries back to the agent.
_PREVIOUS_REPLY_CHARS = 4000


class LockEscalation:
    """Take what a reply asked for beyond its grant, or restart the task."""

    name = "lock_escalation"

    def __init__(self, *, max_escalations: int) -> None:
        self._max = max_escalations

    def check(self, ctx: CommitContext) -> Verdict:
        """Grant, resend, or restart a task whose reply needs more nodes."""
        growth = ctx.growth
        if growth is None or not growth.escalations:
            return ACCEPT
        wanted = growth.escalations
        ctx.wave.escalations_requested += len(wanted)
        rounds = _rounds(ctx)
        if len(rounds) >= self._max:
            self._log(ctx, wanted, "refused", reason=(
                f"session.max_escalations ({self._max}) reached; continuing "
                "without these nodes"
            ))
            return ACCEPT
        rounds.append([str(e.node_id) for e in wanted])
        writes = [e.node_id for e in wanted if e.mode is LockMode.WRITE]
        reads = [e.node_id for e in wanted if e.mode is LockMode.READ]
        if any(e.coarse and e.mode is LockMode.WRITE for e in wanted):
            return self._restart(ctx, wanted, writes, reads, "coarse")
        task = ctx.task
        widened = replace(
            task,
            target_nodes=list(dict.fromkeys([*task.target_nodes, *writes])),
            context_nodes=list(dict.fromkeys([*task.context_nodes, *reads])),
        )
        if task.changes_api is True and task.api_targets:
            widened = replace(
                widened, api_targets=list(dict.fromkeys([*task.api_targets, *writes]))
            )
        requests = [
            (r, m) for r, m in lock_requests(widened, ctx.wave.lock_policy)
            if not ctx.holds(r, m)
        ]
        if not ctx.escalate(requests):
            return self._restart(ctx, wanted, writes, reads, "busy")
        ctx.wave.escalations_granted += len(wanted)
        if all(_seen_current(ctx, e) for e in wanted):
            self._log(ctx, wanted, "granted")
            extra = {
                e.node_id: NewStaging(e.source, ctx.view.kind(e.node_id))
                for e in wanted
                if e.mode is LockMode.WRITE and e.source is not None
            }
            return accept(extra=extra, widen=writes, widen_context=reads)
        self._log(ctx, wanted, "granted_resend")
        names = ", ".join(f"'{e.node_id}'" for e in wanted)
        return resend(
            [f"granted {names} at commit; the agent had not seen its current source"],
            note=(
                f"You asked for {names}, which your task was not given. It is "
                "yours now, and your bundle carries its current source. Redo "
                "the task with it in your target_nodes; if, having seen it, "
                "you no longer need to change it, leave it out. Why it was "
                f"asked for: {'; '.join(dict.fromkeys(e.reason for e in wanted))}."
            ),
            error_kind="escalated",
            widen=writes,
            widen_context=reads,
        )

    def _restart(
        self,
        ctx: CommitContext,
        wanted: list[Escalation],
        writes: list[NodeId],
        reads: list[NodeId],
        why: str,
    ) -> Verdict:
        """Give everything back and re-queue the task with the nodes it needs."""
        names = ", ".join(f"'{e.node_id}'" for e in wanted)
        if why == "coarse":
            cause = (
                "covers nodes other tasks write, so it is never granted "
                "mid-task"
            )
        else:
            cause = "is held by another task"
        self._log(ctx, wanted, "restarted", reason=cause)
        return escalate(
            f"needs {names}, which {cause}; restarted with it in the grant",
            note=(
                f"Your previous reply needed {names}, which your task was not "
                f"given and which {cause}. The task was re-queued with it "
                "added to your target_nodes, and this dispatch holds every "
                "node at once. Do the whole task again against the current "
                "sources; leave the added node out if you no longer need to "
                f"change it.{_previous_reply(ctx)}"
            ),
            targets=writes,
            context=reads,
        )

    @staticmethod
    def _log(
        ctx: CommitContext, wanted: list[Escalation], outcome: str, reason: str = ""
    ) -> None:
        """Record one escalation decision."""
        ctx.log(
            EventType.ESCALATION,
            task_id=ctx.task_id,
            nodes={str(e.node_id): e.mode.value for e in wanted},
            outcome=outcome,
            reasons=[e.reason for e in wanted],
            detail=reason or None,
        )


def _rounds(ctx: CommitContext) -> list[list[str]]:
    """Return the task's escalation history, kept in scheduler annotations.

    The annotations persist with the scheduler, so ``--recover`` neither
    forgets a task's widened grant nor resets its escalation budget.
    """
    annotations = ctx.wave.require_scheduler().annotations
    history = annotations.setdefault("escalations", {})
    assert isinstance(history, dict)
    rounds = history.setdefault(ctx.task_id, [])
    assert isinstance(rounds, list)
    return rounds


def _seen_current(ctx: CommitContext, wanted: Escalation) -> bool:
    """Whether the agent saw the node's full current source when it worked.

    A write also needs a proposal: a declared need without source has
    nothing to stage.
    """
    if wanted.mode is LockMode.WRITE and wanted.source is None:
        return False
    mark = ctx.read_mark(wanted.node_id)
    if mark is None or mark.api_only:
        return False
    current = ctx.view.source(wanted.node_id)
    digest = source_digest(current) if current is not None else None
    return mark.digest == digest


def _previous_reply(ctx: CommitContext) -> str:
    """Return the previous reply's sources, bounded, for a restart's note."""
    sources = ctx.staged_sources()
    growth = ctx.growth
    if growth is not None:
        sources.update({
            e.node_id: e.source for e in growth.escalations if e.source is not None
        })
        sources.update({s.node_id: s.source for s in growth.additive})
    if not sources:
        return ""
    parts = [f"--- {node_id}\n{source}" for node_id, source in sources.items()]
    text = "\n".join(parts)
    if len(text) > _PREVIOUS_REPLY_CHARS:
        text = text[:_PREVIOUS_REPLY_CHARS] + "\n... (truncated)"
    return f" Your previous reply, for reference:\n{text}"
