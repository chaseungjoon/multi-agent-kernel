"""Per-wave state: everything one wave accumulates, created fresh for the next.

A *wave* is one installed plan run to completion (``install_plan`` → ``run``),
and a session can run several — the initial wave, then cascade waves. Anything a
wave accumulates must not leak into the next, so all of it lives here and
``install_plan`` replaces the whole object with :meth:`WaveState.start` rather
than resetting fields one by one: a field added here is fresh per wave by
construction, where a hand-kept reset list silently carried any field it missed.

Fields, by what they record:

- **plan** — ``scheduler`` (DAG + dispatch state), ``graph`` (the reference
  graph the wave was planned against), ``lock_policy``, ``preexisting_files``
  (the files that had committed nodes when the plan was installed),
  ``plan_findings`` and ``progress`` (per-task grant accounting).
- **outcomes** — ``completed``, ``failed``, ``failure_reasons`` (latest reason
  per task), ``failure_history`` (every distinct reason, in order),
  ``partial_queue`` (tasks awaiting a narrowed re-dispatch), ``budget_stop``
  (why the token ceiling stopped the run) and ``wedged`` (a worker stopped
  answering).
- **commits** — ``committed`` (node → (before, after) source; ``None`` after a
  node a whole-file commit removed), ``file_before`` (each touched file before
  its first commit; ``None`` when it did not exist), ``file_writers``,
  ``node_writer``, ``fragments_before`` and ``commit_log`` (what rebuilding
  "pre-wave plus these tasks' commits" needs). Written only past a commit
  point, so a rolled-back transaction leaves no trace.
- **in flight** — ``granted`` (the lock set each task was dispatched under;
  commit re-validation checks those *modes*), ``read_sets`` (every node a
  task's latest bundle carried, at the version it carried it), ``parked``
  (finished results waiting on a lock or a contract provider), ``deferring``
  (the commit being parked right now, and why) and ``waiting_on_providers``.
- **post-wave caches** — ``defects_at``, ``cascade_at``, ``gates_at``: findings
  keyed by store generation, because the cascade loop asks twice for one state.
- **metrics** — the counters :func:`mak.session.results.plan_metrics` reports.

Session-lifetime state (configuration and collaborators, the user objective,
cascade history, token usage, the symbol index, ``.makignore``, the executor,
the last result) stays on the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mak.conflict_detector.cross_module_check import CrossModuleDefect
from mak.core.exceptions import SessionError
from mak.core.types import LockMode, NodeId, SubTask, TaskBundle, TaskResult
from mak.planner.depgraph import DepGraph
from mak.planner.validation import PlanFinding
from mak.scheduler.lock_policy import LEGACY_POLICY, LockPolicy
from mak.scheduler.scheduler import Scheduler
from mak.semantic.cascade_graph import CascadeItem
from mak.semantic.gate_types import GateFinding
from mak.semantic.read_set import ReadSet
from mak.session.types import SubTaskProgress


@dataclass(frozen=True, slots=True)
class Parked:
    """A finished result waiting for a lock another in-flight task holds.

    ``sources`` are the staged sources at the moment of parking: the store's
    pending slot for a node is single, and a registrar other tasks append to
    would otherwise have this task's staging overwritten while it waits.
    """

    bundle: TaskBundle
    result: TaskResult
    sources: dict[NodeId, str]
    reason: str
    # Waiting for a contract provider to commit, not for a lock.
    on_providers: bool = False


@dataclass
class WaveState:
    """Everything one wave accumulates; see the module docstring for each field."""

    # -- plan
    scheduler: Scheduler | None = None
    graph: DepGraph | None = None
    lock_policy: LockPolicy = LEGACY_POLICY
    preexisting_files: set[str] = field(default_factory=set)
    plan_findings: list[PlanFinding] = field(default_factory=list)
    progress: dict[str, SubTaskProgress] = field(default_factory=dict)
    # -- outcomes
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    failure_reasons: dict[str, str] = field(default_factory=dict)
    failure_history: dict[str, list[str]] = field(default_factory=dict)
    partial_queue: list[str] = field(default_factory=list)
    budget_stop: str | None = None
    wedged: bool = False
    # -- commits
    committed: dict[NodeId, tuple[str | None, str | None]] = field(
        default_factory=dict
    )
    file_before: dict[str, str | None] = field(default_factory=dict)
    file_writers: dict[str, list[str]] = field(default_factory=dict)
    node_writer: dict[NodeId, str] = field(default_factory=dict)
    fragments_before: dict[str, list[tuple[NodeId, int | None, str]]] = field(
        default_factory=dict
    )
    commit_log: list[tuple[str, NodeId, int | None, str]] = field(
        default_factory=list
    )
    # -- in flight
    granted: dict[str, dict[NodeId, LockMode]] = field(default_factory=dict)
    read_sets: dict[str, ReadSet] = field(default_factory=dict)
    parked: dict[str, Parked] = field(default_factory=dict)
    deferring: dict[str, str] = field(default_factory=dict)
    waiting_on_providers: set[str] = field(default_factory=set)
    # -- post-wave caches, keyed by store generation
    defects_at: tuple[int, list[CrossModuleDefect]] | None = None
    cascade_at: tuple[int, list[CascadeItem]] | None = None
    gates_at: tuple[int, list[GateFinding]] | None = None
    # -- metrics
    conflict_rejections: int = 0
    redispatches: int = 0
    concurrency_samples: list[int] = field(default_factory=list)
    dispatches: int = 0
    context_bytes: int = 0
    starved_dispatches: int = 0
    stale_reads: int = 0
    stale_redispatches: int = 0
    adjudicated_accepts: int = 0

    @classmethod
    def start(
        cls,
        *,
        preexisting_files: set[str] | None = None,
        scheduler: Scheduler | None = None,
        graph: DepGraph | None = None,
        lock_policy: LockPolicy = LEGACY_POLICY,
        progress: dict[str, SubTaskProgress] | None = None,
        read_sets: dict[str, ReadSet] | None = None,
    ) -> WaveState:
        """Begin a wave with nothing accumulated yet."""
        return cls(
            scheduler=scheduler,
            graph=graph,
            lock_policy=lock_policy,
            preexisting_files=set(preexisting_files or ()),
            progress=dict(progress or {}),
            read_sets=dict(read_sets or {}),
        )

    def require_scheduler(self) -> Scheduler:
        """Return the wave's scheduler, or raise when no plan is installed."""
        if self.scheduler is None:
            raise SessionError("no plan installed; call plan() or install_plan() first")
        return self.scheduler

    def task(self, task_id: str) -> SubTask:
        """Return a task of this wave's plan by id."""
        return self.require_scheduler().dag.get_task(task_id)
