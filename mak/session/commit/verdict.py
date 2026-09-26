"""The contract between the commit pipeline and each check it runs.

A check reads the commit in progress through a :class:`CommitContext` and says
what should happen to it with a :class:`Verdict`. It does not roll anything
back, record failures, write kernel notes or park results — the pipeline's
verdict handlers own every such consequence, so the same verdict always means
the same thing whichever check returned it. Two effects stay with the check,
because they are part of the question it asks: logging its own findings, and
escalating a lock through :meth:`CommitContext.escalate` (a try-acquire *is*
the test of whether the commit may proceed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from mak.core.types import LockMode, NodeId, SubTask
from mak.semantic.read_set import ReadMark
from mak.session.events import EventLog
from mak.session.grants import granted_mode
from mak.session.store_view import StoreView
from mak.session.types import LockTableLike, SubTaskProgress
from mak.session.wave import WaveState

VerdictKind = Literal["accept", "reject", "defer", "resend", "fail"]


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the pipeline does with a commit after one check.

    - ``accept`` — continue to the next check (or commit, after the last);
    - ``reject`` — a conflict: roll back, count it, retry within the budget;
    - ``defer`` — the work is fine but must wait (for a lock another in-flight
      task holds, or for a contract provider to commit): park the result;
    - ``resend`` — the kernel declined for a reason the agent can act on: roll
      back and re-dispatch with ``retry_note`` (``error_kind`` names the case);
    - ``fail`` — the task cannot succeed at all: roll back and exhaust its
      attempts.

    ``restaged`` carries sources the check rewrote (a registrar append merged
    onto the table's current content); the pipeline stages them before it acts
    on the verdict, whatever its kind.
    """

    kind: VerdictKind
    reasons: tuple[str, ...] = ()
    retry_note: str | None = None
    error_kind: str | None = None
    waiting_on: Literal["lock", "providers"] | None = None
    restaged: Mapping[NodeId, str] | None = None

    @property
    def reason(self) -> str:
        """Every reason, joined as one failure-log entry."""
        return "; ".join(self.reasons)


ACCEPT = Verdict("accept")


def accept(restaged: Mapping[NodeId, str] | None = None) -> Verdict:
    """Let the commit continue, staging ``restaged`` first when given."""
    return Verdict("accept", restaged=restaged or None)


def reject(reasons: list[str] | tuple[str, ...]) -> Verdict:
    """Refuse the commit as a conflict."""
    return Verdict("reject", reasons=tuple(reasons))


def defer(
    reason: str,
    *,
    providers: bool = False,
    restaged: Mapping[NodeId, str] | None = None,
) -> Verdict:
    """Park the result until the lock (or contract provider) it waits on frees."""
    return Verdict(
        "defer",
        reasons=(reason,),
        waiting_on="providers" if providers else "lock",
        restaged=restaged or None,
    )


def resend(
    reasons: list[str] | tuple[str, ...],
    *,
    note: str,
    error_kind: str,
    restaged: Mapping[NodeId, str] | None = None,
) -> Verdict:
    """Roll back and re-dispatch with a kernel note the agent can act on."""
    return Verdict(
        "resend",
        reasons=tuple(reasons),
        retry_note=note,
        error_kind=error_kind,
        restaged=restaged or None,
    )


def fail(reason: str) -> Verdict:
    """Give up on the task: roll back and spend its remaining attempts."""
    return Verdict("fail", reasons=(reason,))


@dataclass
class CommitContext:
    """One commit in progress, as every check sees it.

    ``staged`` are the task's pending node ids and ``peers`` the sources already
    committed earlier in the same batch. ``wave``, ``view`` and ``lock_table``
    are for reading; the only mutation a check may make is :meth:`escalate`.
    """

    task_id: str
    staged: list[NodeId]
    peers: Mapping[str, str]
    wave: WaveState
    view: StoreView
    lock_table: LockTableLike
    log: EventLog
    _task: SubTask | None = field(default=None, repr=False)

    @property
    def task(self) -> SubTask:
        """The plan's task being committed (looked up once, on first use)."""
        task = self._task
        if task is None:
            task = self._task = self.wave.task(self.task_id)
        return task

    @property
    def progress(self) -> SubTaskProgress:
        """The task's grant accounting."""
        return self.wave.progress[self.task_id]

    def granted_mode(self, node_id: NodeId) -> LockMode:
        """Return the mode the task holds ``node_id`` in (WRITE if unrecorded)."""
        return granted_mode(self.wave, self.task_id, node_id)

    def read_mark(self, node_id: NodeId) -> ReadMark | None:
        """How ``node_id`` looked when the task's latest bundle carried it."""
        return self.wave.read_sets.get(self.task_id, {}).get(node_id)

    def staged_sources(self) -> dict[NodeId, str]:
        """Return the task's pending sources, keyed by node id."""
        return self.view.staged_sources(self.staged)

    def escalate(self, requests: list[tuple[NodeId, LockMode]]) -> bool:
        """Take ``requests`` for the task now, or report that they are held.

        On success the new modes join the task's grant, so commit-time lease
        re-validation and release both see them.
        """
        if not self.lock_table.try_acquire_all(requests, self.task_id):
            return False
        self.wave.granted.setdefault(self.task_id, {}).update(dict(requests))
        return True


class CommitCheck(Protocol):
    """One commit-time check: a name and a verdict on a commit in progress."""

    name: str

    def check(self, ctx: CommitContext) -> Verdict:
        """Return what the pipeline should do with this commit."""
        ...
