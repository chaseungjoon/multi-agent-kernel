"""The contract between the commit pipeline and each check it runs.

A check reads the commit in progress through a :class:`CommitContext` and says
what should happen to it with a :class:`Verdict`. It does not roll anything
back, record failures, write kernel notes or park results — the pipeline's
verdict handlers own every such consequence, so the same verdict always means
the same thing whichever check returned it. Two effects stay with the check,
because they are part of the question it asks: logging its own findings, and
taking a lock through :meth:`CommitContext.escalate` or
:meth:`CommitContext.acquire_transient` (a try-acquire *is* the test of
whether the commit may proceed).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from mak.core.types import LockMode, NodeId, SubTask
from mak.semantic.read_set import ReadMark
from mak.session.events import EventLog
from mak.session.grants import granted_mode
from mak.session.store_view import StoreView
from mak.session.types import Growth, LockTableLike, SubTaskProgress
from mak.session.wave import WaveState

VerdictKind = Literal["accept", "reject", "defer", "resend", "fail", "escalate"]

_RANK = {LockMode.READ: 1, LockMode.INTENT_WRITE: 2, LockMode.WRITE: 3}


@dataclass(frozen=True, slots=True)
class NewStaging:
    """A node the kernel stages for the commit (not one the agent was granted).

    A merged module header, a new helper, or an escalated node. ``place`` puts
    a node the store has never seen right after ``after`` (``None``: first in
    its file) instead of at the end.
    """

    source: str
    kind: str
    after: NodeId | None = None
    place: bool = False


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
      attempts;
    - ``escalate`` — the task needs nodes another task holds: give back every
      lock, widen the task by ``widen``/``widen_context``, and re-queue it so
      the scheduler acquires the whole set atomically (never hold and wait).

    ``restaged`` carries sources the check rewrote (a registrar append merged
    onto the table's current content); the pipeline stages them before it acts
    on the verdict, whatever its kind. ``extra`` are nodes the check adds to
    the commit; they are rolled back with it. ``widen``/``widen_context`` on
    any other kind grow the task's targets and context in place.
    """

    kind: VerdictKind
    reasons: tuple[str, ...] = ()
    retry_note: str | None = None
    error_kind: str | None = None
    waiting_on: Literal["lock", "providers"] | None = None
    restaged: Mapping[NodeId, str] | None = None
    extra: Mapping[NodeId, NewStaging] | None = None
    widen: tuple[NodeId, ...] = ()
    widen_context: tuple[NodeId, ...] = ()

    @property
    def reason(self) -> str:
        """Every reason, joined as one failure-log entry."""
        return "; ".join(self.reasons)


ACCEPT = Verdict("accept")


def accept(
    restaged: Mapping[NodeId, str] | None = None,
    *,
    extra: Mapping[NodeId, NewStaging] | None = None,
    widen: Sequence[NodeId] = (),
    widen_context: Sequence[NodeId] = (),
) -> Verdict:
    """Let the commit continue, staging ``restaged`` and ``extra`` first."""
    return Verdict(
        "accept",
        restaged=restaged or None,
        extra=extra or None,
        widen=tuple(widen),
        widen_context=tuple(widen_context),
    )


def escalate(
    reason: str,
    *,
    note: str,
    targets: Sequence[NodeId],
    context: Sequence[NodeId] = (),
) -> Verdict:
    """Restart the task with a wider grant instead of waiting for one."""
    return Verdict(
        "escalate",
        reasons=(reason,),
        retry_note=note,
        error_kind="escalation_restart",
        widen=tuple(targets),
        widen_context=tuple(context),
    )


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
    widen: Sequence[NodeId] = (),
    widen_context: Sequence[NodeId] = (),
) -> Verdict:
    """Roll back and re-dispatch with a kernel note the agent can act on."""
    return Verdict(
        "resend",
        reasons=tuple(reasons),
        retry_note=note,
        error_kind=error_kind,
        restaged=restaged or None,
        widen=tuple(widen),
        widen_context=tuple(widen_context),
    )


def fail(reason: str) -> Verdict:
    """Give up on the task: roll back and spend its remaining attempts."""
    return Verdict("fail", reasons=(reason,))


@dataclass
class CommitContext:
    """One commit in progress, as every check sees it.

    ``staged`` are the task's pending node ids and ``peers`` the sources already
    committed earlier in the same batch. ``wave``, ``view`` and ``lock_table``
    are for reading; the only mutations a check may make are :meth:`escalate`,
    :meth:`acquire_transient` and queueing :attr:`on_commit` callbacks.

    ``extras`` are the ids the kernel staged into this commit (see
    :class:`NewStaging`), and ``transient`` the locks taken for this commit
    alone; the pipeline releases them when the commit ends either way.
    ``on_commit`` runs only once the commit is durable: a check counts and logs
    what it merged there, so a deferred or rolled-back attempt counts nothing.
    """

    task_id: str
    staged: list[NodeId]
    peers: Mapping[str, str]
    wave: WaveState
    view: StoreView
    lock_table: LockTableLike
    log: EventLog
    extras: list[NodeId] = field(default_factory=list)
    transient: list[tuple[NodeId, LockMode]] = field(default_factory=list)
    on_commit: list[Callable[[], None]] = field(default_factory=list)
    _task: SubTask | None = field(default=None, repr=False)

    @property
    def task(self) -> SubTask:
        """The plan's task being committed (looked up once, on first use)."""
        task = self._task
        if task is None:
            task = self._task = self.wave.task(self.task_id)
        return task

    def refresh_task(self) -> None:
        """Forget the cached task after the plan's definition of it changed."""
        self._task = None

    @property
    def growth(self) -> Growth | None:
        """What the reply asked for beyond its grant, if anything."""
        growth = self.wave.growth.get(self.task_id)
        return None if growth is None or growth.empty else growth

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

    def holds(self, resource: NodeId, mode: LockMode) -> bool:
        """Whether the task already holds ``resource`` at least as strongly."""
        held = self.wave.granted.get(self.task_id, {}).get(resource)
        if held is not None and _RANK[held] >= _RANK[mode]:
            return True
        return any(
            r == resource and _RANK[m] >= _RANK[mode] for r, m in self.transient
        )

    def acquire_transient(self, requests: list[tuple[NodeId, LockMode]]) -> bool:
        """Take ``requests`` for this commit only, or report that one is busy.

        Unlike :meth:`escalate` these do not join the task's grant: they guard
        the merge or creation this commit performs and are released when it
        ends. Resources the task already holds are skipped.
        """
        needed = [(r, m) for r, m in dict(requests).items() if not self.holds(r, m)]
        if not needed:
            return True
        if not self.lock_table.try_acquire_all(needed, self.task_id):
            return False
        self.transient.extend(needed)
        return True

    def release_transient(self) -> None:
        """Give back every lock :meth:`acquire_transient` took."""
        for resource, mode in self.transient:
            self.lock_table.release(resource, mode, self.task_id)
        self.transient.clear()


class CommitCheck(Protocol):
    """One commit-time check: a name and a verdict on a commit in progress."""

    name: str

    def check(self, ctx: CommitContext) -> Verdict:
        """Return what the pipeline should do with this commit."""
        ...
