"""The session's public result types and the protocols it depends on."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from mak.core.types import LockEntry, LockMode, NodeId, SubTask
from mak.planner.validation import PlanFinding

# A test runner returns (passed, output) so teardown can gate the push.
TestRunner = Callable[[], tuple[bool, str]]


class Assigner(Protocol):
    """Anything exposing the agent runner's ``assign`` entry point."""

    def assign(self, adapter: object, task: object) -> object:
        """Dispatch a task bundle to an adapter and return a result."""
        ...


class LockTableLike(Protocol):
    """The subset of ``LockTable`` the session and its scheduler depend on."""

    def try_acquire_all(
        self, requests: list[tuple[NodeId, LockMode]], holder: str
    ) -> bool:
        """Atomically acquire every requested lock, or none."""
        ...

    def release(self, node_id: NodeId, mode: LockMode, holder: str) -> bool:
        """Release a single held lock."""
        ...

    def release_all(self, holder: str) -> int:
        """Release every lock held by ``holder``."""
        ...

    def clear(self) -> int:
        """Drop every lease (stale leases from a prior session); return the count."""
        ...

    def expire_stale(self) -> list[LockEntry]:
        """Expire and return timed-out leases."""
        ...

    def holds_all(
        self, requests: list[tuple[NodeId, LockMode]], holder: str
    ) -> bool:
        """Whether ``holder`` still holds every requested lease (expiry-aware)."""
        ...

    def renew_all(self, holder: str) -> int:
        """Heartbeat every lease held by ``holder``; return the count renewed."""
        ...

    def all_entries(self) -> dict[NodeId, list[LockEntry]]:
        """Return a copy of the full lock table."""
        ...


class SessionState(StrEnum):
    """Lifecycle phases of a session."""

    CREATED = "created"
    INITIALIZED = "initialized"
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class SubTaskProgress:
    """Per-task completion state, tracking which node grants are done."""

    task_id: str
    target_nodes: list[NodeId]
    completed_nodes: set[NodeId] = field(default_factory=set)
    attempts: int = 0
    # Grants closed because the agent asserted no change was needed, rather than
    # because it returned work. Tracked so a run can report the two separately.
    noop_nodes: set[NodeId] = field(default_factory=set)
    # Why the previous attempt produced nothing, phrased as an instruction for the
    # next one. Carried onto the re-dispatched bundle so a retry differs from the
    # attempt that failed instead of re-issuing it verbatim.
    retry_note: str | None = None
    # Set when the kernel — not the agent — sent the attempt back: a stale read
    # (the diff of what changed) or a broken interface promise. It outranks
    # every agent-side reason, because it is the one thing the next attempt
    # must act on, and ``error_kind`` says which it was.
    kernel_note: str | None = None
    error_kind: str | None = None

    @property
    def remaining(self) -> list[NodeId]:
        """Target nodes not yet committed, in original order."""
        return [n for n in self.target_nodes if n not in self.completed_nodes]

    @property
    def is_complete(self) -> bool:
        """True once every target node has been committed."""
        return all(n in self.completed_nodes for n in self.target_nodes)


@dataclass(frozen=True, slots=True)
class SessionResult:
    """The outcome of a ``run``.

    A task that was neither completed nor explicitly failed is reported as one of:

    - ``skipped`` — it (transitively) depended on a task that **failed**, so it could
      never have run. This is a downstream consequence of a real failure, not an
      independent problem.
    - ``blocked`` — it was stranded for some *other* reason (locks that never freed,
      a wedged worker), with no failed ancestor to explain it.

    A run with any failed, skipped, or blocked tasks ends in ``FAILED``.
    """

    state: SessionState
    completed: tuple[str, ...]
    failed: tuple[str, ...]
    blocked: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    # The subset of ``completed`` that closed because the agent asserted no change
    # was needed. These are completions, not failures — but "4 completed" reads as
    # four files changed, so the two claims are reported apart.
    noop: tuple[str, ...] = ()
    failure_reasons: dict[str, str] = field(default_factory=dict)
    # Plan-quality metrics for this run (realized parallelism, conflict/redispatch
    # rate). Empty for a session that never ran. See ``mak.session.results``.
    metrics: dict[str, float] = field(default_factory=dict)
    # Why the run stopped before the DAG was done, when the cause was the run
    # itself rather than any one task — today only the token budget. Reported
    # separately because the tasks it strands have no failure of their own to
    # explain them, and "3 blocked" with no reason is not an answer.
    stopped_reason: str | None = None

    @property
    def ok(self) -> bool:
        """True only when the run completed with nothing failed/blocked/skipped."""
        return (
            self.state is SessionState.COMPLETED
            and not self.failed
            and not self.blocked
            and not self.skipped
            and self.stopped_reason is None
        )


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """A validated plan the user has not reviewed yet (``Session.propose_plan``).

    ``subtasks`` is the plan after deterministic validation grounded and
    augmented it; ``findings`` says what validation changed.
    """

    subtasks: list[SubTask]
    findings: list[PlanFinding]
