"""Session: orchestrate the full MAK pipeline over a node store and lock table.

A ``Session`` drives the end-to-end flow: **init** ingests the
codebase into the node store; **run** plans the work (optionally with HitL review),
then loops the scheduler — dispatching tasks, validating each agent's staged
fragments with the conflict detector, committing on success, reconstructing the
affected files, and recording an audit commit; **teardown** runs the test suite and
pushes if green.

**Concurrency (Wave 5).** ``run`` dispatches every lock-satisfiable ready task onto
a bounded thread pool (``max_concurrent_agents``) instead of running one agent to
completion before the next. Results are collected as they arrive and **batched**:
all results that complete around the same time are validated together so the
conflict detector finally sees *cross-agent* edits (a signature change in one task
versus a call in another, a symbol two tasks both introduce). Within a batch,
commits are applied in a deterministic order — topological index, then task id —
and each task is validated against the fragments already committed earlier in the
same batch, so when two tasks genuinely conflict the earlier one wins and the later
one is rejected and retried. Two safety nets run alongside the loop: a **heartbeat**
renews every in-flight task's leases so a slow-but-alive agent is never expired, and
a **deadlock watchdog** scans the wait graph (atomic lock pre-allocation makes a
cycle impossible, so this is defense in depth).

Two robustness features sit on top of the basic loop:

- **Crash recovery** (``recover``): on startup a stale ``.mak/lock_table.json`` is
  expired (releasing dead holders' leases) and incomplete tasks are re-queued from
  ``.mak/task_graph.json`` via ``Scheduler.from_persisted``.
- **Partial completion**: an agent that finishes only some of its node grants
  (``modified_nodes`` ⊊ ``target_nodes``) has the completed grants accepted and
  committed; only the *remaining* grants are re-dispatched as a narrower task
  (tracked per task by ``SubTaskProgress``), instead of redoing the whole task.

The collaborators (node store, lock table, registry, agent runner, conflict
detector, planner, git helper, logger) are injected so the session is testable with
fakes and is not bound to concrete subprocess/LLM backends.
"""

from __future__ import annotations

import ast
import fnmatch
import queue
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from mak.agent_runner.adapters.budget import TRUNCATION_STOP_REASONS
from mak.agent_runner.protocol import map_returned_sources
from mak.agent_runner.registry import AdapterRegistry
from mak.agent_runner.stop_signals import matches
from mak.config import MakConfig
from mak.conflict_detector.cross_module_check import (
    CrossModuleDefect,
    check_cross_module_api,
)
from mak.conflict_detector.detector import ConflictDetector, EditRound
from mak.core.exceptions import NodeStoreError, SessionError
from mak.core.logging import EventType, SessionLogger
from mak.core.types import (
    LockEntry,
    LockMode,
    NodeFragment,
    NodeId,
    SubTask,
    TaskBundle,
    TaskResult,
)
from mak.git_integration.git import GitHelper
from mak.lock_manager.deadlock_detector import DeadlockDetector
from mak.node_store.api_digest import public_api_digest
from mak.node_store.reconstruction import assemble_fragments, reconstruct_file
from mak.node_store.store import NodeStore
from mak.planner.depgraph import dep_graph_from_store
from mak.planner.planner import Planner
from mak.planner.review import display_plan_for_review
from mak.planner.validation import PlanFinding, validate_plan
from mak.scheduler.dag import DAG
from mak.scheduler.scheduler import Scheduler

# A test runner returns (passed, output) so teardown can gate the push.
TestRunner = Callable[[], tuple[bool, str]]


class _Assigner(Protocol):
    """Anything exposing the agent runner's ``assign`` entry point."""

    def assign(self, adapter: object, task: object) -> object:
        """Dispatch a task bundle to an adapter and return a result."""
        ...


class _LockTableLike(Protocol):
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
    # rate). Empty for a session that never ran. See ``Session._finalize``.
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True only when the run completed with nothing failed/blocked/skipped."""
        return (
            self.state is SessionState.COMPLETED
            and not self.failed
            and not self.blocked
            and not self.skipped
        )


@dataclass(frozen=True, slots=True)
class _Completion:
    """One finished agent call: the bundle that was dispatched and its result."""

    bundle: TaskBundle
    result: TaskResult


@dataclass(frozen=True, slots=True)
class _Dispatch:
    """An enriched bundle, or the reason the kernel must not send it to an agent.

    ``starved_reason`` is set when enrichment produced *no context at all* for a
    task that declares dependencies or context nodes. That is a kernel defect, not
    an agent failure: the model would be asked to write code against APIs it has
    never been shown, and the only way anyone learned it had happened was an agent
    honest enough to refuse. The bundle is kept so the completion still names the
    task it belongs to.
    """

    bundle: TaskBundle
    starved_reason: str | None = None


class _ConcurrentRunner:
    """Enriches a bundle, runs the agent on a worker thread, queues the result.

    The scheduler calls ``assign`` synchronously during ``tick``; this wrapper
    makes it non-blocking by submitting the real agent call to a thread pool, so a
    single ``tick`` fans out every lock-satisfiable ready task concurrently. The
    bundle is enriched with source context on the *calling* thread (the node store
    read happens before the agent runs, and the write targets are write-locked, so
    the snapshot is stable); the agent then runs on a pool thread, and the finished
    ``(bundle, result)`` pair is pushed onto ``completions`` for the session to
    collect. An agent that raises is converted into a failed ``TaskResult`` so a
    crash never strands the collector waiting on a result that never comes.
    """

    def __init__(
        self,
        inner: _Assigner,
        executor: ThreadPoolExecutor,
        completions: queue.Queue[_Completion],
        enrich: Callable[[TaskBundle], _Dispatch],
    ) -> None:
        self._inner = inner
        self._executor = executor
        self._completions = completions
        self._enrich = enrich

    def assign(self, adapter: object, task: object) -> object:
        dispatch = self._enrich(cast(TaskBundle, task))
        if dispatch.starved_reason is not None:
            # Never spend a model call on a bundle the kernel knows is empty:
            # queue the failure directly so it flows through the normal reporting
            # path, unretryable because a re-dispatch would build the same bundle.
            self._completions.put(_Completion(
                dispatch.bundle,
                TaskResult(
                    task_id=dispatch.bundle.task_id,
                    success=False,
                    error=dispatch.starved_reason,
                    retryable=False,
                ),
            ))
            return None
        self._executor.submit(self._run, adapter, dispatch.bundle)
        return None

    def _run(self, adapter: object, bundle: TaskBundle) -> None:
        try:
            result = cast(TaskResult, self._inner.assign(adapter, bundle))
        except Exception as exc:  # surface any agent failure as a result, not a hang
            result = TaskResult(
                task_id=bundle.task_id,
                success=False,
                modified_nodes=[],
                error=str(exc),
            )
        self._completions.put(_Completion(bundle, result))


class Session:
    """Orchestrates init → plan → run → teardown over injected subsystems."""

    def __init__(
        self,
        *,
        session_id: str,
        config: MakConfig,
        node_store: NodeStore,
        lock_table: _LockTableLike,
        registry: AdapterRegistry,
        agent_runner: _Assigner,
        conflict_detector: ConflictDetector | None = None,
        deadlock_detector: DeadlockDetector | None = None,
        planner: Planner | None = None,
        git_helper: GitHelper | None = None,
        logger: SessionLogger | None = None,
        test_runner: TestRunner | None = None,
        max_attempts: int = 3,
        default_agent_type: str | None = None,
        agent_pool: list[str] | None = None,
        heartbeat_interval_s: float | None = None,
        collect_timeout_s: float = 300.0,
    ) -> None:
        self.session_id = session_id
        self._config = config
        self._default_agent_type = default_agent_type
        # Healthy configured agent types to distribute unassigned tasks across
        # (round-robin). Falls back to [default_agent_type] when not provided.
        self._agent_pool = list(agent_pool) if agent_pool else None
        self._node_store = node_store
        self._lock_table = lock_table
        self._registry = registry
        self._agent_runner = agent_runner
        self._conflict_detector = conflict_detector or ConflictDetector()
        self._deadlock_detector = deadlock_detector or DeadlockDetector()
        self._planner = planner
        self._git = git_helper
        self._logger = logger
        self._test_runner = test_runner
        self._max_attempts = max_attempts

        self._max_concurrent = max(1, config.session.max_concurrent_agents)
        self._collect_timeout = collect_timeout_s
        self._deadlock_interval = config.session.deadlock_check_interval_s
        self._heartbeat_interval = (
            heartbeat_interval_s
            if heartbeat_interval_s is not None
            else max(1.0, config.session.lock_timeout_s / 3.0)
        )

        self.state = SessionState.CREATED
        self._scheduler: Scheduler | None = None
        self._progress: dict[str, SubTaskProgress] = {}
        self._completions: queue.Queue[_Completion] = queue.Queue()
        self._executor: ThreadPoolExecutor | None = None
        self._concurrent_runner: _ConcurrentRunner | None = None
        self._partial_queue: list[str] = []
        self._completed: list[str] = []
        self._failed: list[str] = []
        # Most recent reason a task did not make progress (agent error or a
        # rejection reason), surfaced on the result so a failure is diagnosable.
        self._failure_reasons: dict[str, str] = {}
        # Every distinct reason, in attempt order. A task can fail differently on
        # each attempt, and reporting only the last one hides the cause: a run
        # whose real defect rejected attempts 1-2 reported only attempt 3's
        # one-off malformed response, which named nothing relevant.
        self._failure_history: dict[str, list[str]] = {}
        # Per-wave commit log: node_id → (source_before, source_after).
        # Populated during run(); read by detect_cascade_tasks() after run().
        self._wave_committed: dict[NodeId, tuple[str | None, str]] = {}
        # Deterministic plan-validation findings from the most recent install_plan;
        # surfaced to the review UI and available to callers after planning.
        self.last_plan_findings: list[PlanFinding] = []
        # Plan-quality counters, reset per wave in install_plan and reported by
        # _finalize: conflict rejections, partial re-dispatches, and a per-tick
        # sample of in-flight task count (realized parallelism).
        self._conflict_rejections = 0
        self._redispatches = 0
        self._concurrency_samples: list[int] = []
        # Context volume actually dispatched this wave. A bundle's context is the
        # only thing an agent knows about the codebase, so how much of it each
        # attempt received is a first-class run statistic — and ``starved`` counts
        # the dispatches the kernel refused because there was none.
        self._dispatches = 0
        self._context_bytes = 0
        self._starved_dispatches = 0

    # -- logging helper ----------------------------------------------------

    def _log(self, event: EventType, **payload: object) -> None:
        if self._logger is not None:
            self._logger.log(event, session_id=self.session_id, **payload)

    @property
    def _work_dir(self) -> Path:
        return Path(self._config.session.work_dir)

    @property
    def _mak_dir(self) -> Path:
        return Path(self._config.session.mak_dir)

    def _runner(self) -> _ConcurrentRunner:
        """Lazily build the thread-pool-backed runner (and its executor)."""
        if self._concurrent_runner is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_concurrent,
                thread_name_prefix=f"mak-{self.session_id}",
            )
            self._concurrent_runner = _ConcurrentRunner(
                self._agent_runner,
                self._executor,
                self._completions,
                self._enrich_bundle,
            )
        return self._concurrent_runner

    def close(self) -> None:
        """Shut down the worker pool. Safe to call repeatedly."""
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
            self._concurrent_runner = None

    # -- phase 1: initialize ----------------------------------------------

    def initialize(self) -> list[NodeId]:
        """Ingest the working directory's Python files into the node store."""
        if self.state is not SessionState.CREATED:
            raise SessionError(f"cannot initialize from state {self.state}")
        # A fresh session owns none of the leases a prior (possibly killed) run left
        # in the persisted lock table; drop them so they don't surface later as
        # spurious "lease expired" warnings. Crash recovery uses recover() instead.
        self._lock_table.clear()
        pruned = self.prune_excluded_nodes()
        self._ingest_work_dir()
        if self._git is not None and self._config.git.auto_commit:
            # Keep MAK's audit commits inside the project: if the work-dir is nested
            # in an outer repo (e.g. a home directory) or in none at all, give it its
            # own repo so commits never leak into the surrounding one.
            if self._git.ensure_initialized():
                print(
                    f"mak: initialized a git repo in {self._work_dir} for MAK's "
                    "audit log (it was not its own repository).",
                    file=sys.stderr,
                )
        self.state = SessionState.INITIALIZED
        inventory = self._node_store.list_nodes()
        self._log(
            EventType.SESSION_STARTED,
            node_count=len(inventory),
            pruned_nodes=pruned,
        )
        return inventory

    def _ingest_work_dir(self) -> None:
        """Parse every included, non-excluded Python file under the work dir."""
        ns_cfg = self._config.node_store
        for pattern in ns_cfg.include_patterns:
            for path in sorted(self._work_dir.glob(pattern)):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(self._work_dir))
                if self._is_store_path(path) or _is_excluded(
                    rel, ns_cfg.exclude_patterns
                ):
                    continue
                try:
                    self._node_store.parse_file_into_nodes(
                        rel, path.read_text(encoding="utf-8")
                    )
                except SyntaxError:
                    continue

    def _mak_roots(self) -> tuple[Path, ...]:
        """Absolute locations MAK's own persistence directory can resolve to.

        ``mak_dir`` is usually relative (``.mak``). The runtime resolves it
        against the process's working directory, but a store written by an
        earlier run launched *from inside* the project sits under the work dir,
        so both readings are treated as MAK's own.
        """
        mak_dir = self._mak_dir
        if mak_dir.is_absolute():
            candidates = [mak_dir]
        else:
            candidates = [Path.cwd() / mak_dir, self._work_dir / mak_dir]
        roots: list[Path] = []
        for candidate in candidates:
            try:
                roots.append(candidate.resolve())
            except OSError:
                continue
        return tuple(roots)

    def _is_store_path(self, path: Path) -> bool:
        """Whether ``path`` lives inside MAK's own persistence directory.

        Deliberately independent of ``exclude_patterns``: the node store writes
        fragments as ``.py`` files, so ingesting it feeds MAK its own previous
        output back as "source" — a defect that compounds by hundreds of nodes
        per run. A user config that overrides the pattern list must not be able
        to switch this off, because the store is never project source under any
        configuration.
        """
        roots = self._mak_roots()
        if not roots:
            return False
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return any(resolved.is_relative_to(root) for root in roots)

    def prune_excluded_nodes(self) -> int:
        """Drop stored nodes whose file is no longer ingestable; return the count.

        Migration path for stores poisoned before the exclusions above existed:
        a fix-forward run would otherwise keep carrying every fragment MAK had
        ingested from its own ``.mak/`` directory (89% of the store in the run
        that motivated Wave 11). Deleting ``.mak/`` by hand is the blunt
        alternative; this is the one that preserves real work.
        """
        patterns = self._config.node_store.exclude_patterns
        doomed = [
            node_id
            for node_id in self._node_store.list_all_nodes()
            if self._is_excluded_node(str(node_id), patterns)
        ]
        for node_id in doomed:
            self._node_store.remove_node(node_id)
        if doomed:
            print(
                f"mak: pruned {len(doomed)} node(s) that are no longer ingestable "
                "(MAK's own .mak/ store, or an excluded path).",
                file=sys.stderr,
            )
        return len(doomed)

    def _is_excluded_node(self, node_id: str, patterns: tuple[str, ...]) -> bool:
        """Whether a node's file component is excluded from ingestion."""
        file_path = node_id.split("::", 1)[0]
        return self._is_store_path(self._work_dir / file_path) or _is_excluded(
            file_path, patterns
        )

    # -- phase 2: plan -----------------------------------------------------

    def plan(
        self,
        user_task: str,
        *,
        review: bool = True,
        prompt_fn: Callable[[str], str] = input,
        printer: Callable[[str], None] = print,
    ) -> list[SubTask]:
        """Decompose ``user_task`` with the planner, optionally review, and install."""
        if self.state is not SessionState.INITIALIZED:
            raise SessionError(f"cannot plan from state {self.state}")
        if self._planner is None:
            raise SessionError("no planner configured; use install_plan() instead")
        decomposed = self._planner.decompose(
            user_task, self._node_store.list_nodes()
        )
        # Ground and augment the plan before review so the reviewer sees the
        # corrected plan and exactly what validation changed. install_plan()
        # re-validates (an idempotent no-op on the already-corrected plan).
        validated, findings = self._validate_subtasks(decomposed)
        reviewed = validated
        if review:
            reviewed = display_plan_for_review(
                validated, findings=findings, prompt_fn=prompt_fn, printer=printer
            )
        self.install_plan(reviewed)
        if reviewed is validated:
            # Not edited: keep the richer first-pass findings (install_plan's
            # re-validation of an already-corrected plan reports fewer).
            self.last_plan_findings = findings
        return reviewed

    def _validate_subtasks(
        self, subtasks: list[SubTask]
    ) -> tuple[list[SubTask], list[PlanFinding]]:
        """Run deterministic plan validation, unless disabled in config."""
        if not self._config.planner.validate:
            return subtasks, []
        graph = dep_graph_from_store(self._node_store)
        result = validate_plan(subtasks, graph, self._node_store.list_nodes())
        return result.plan, result.findings

    def _log_plan_findings(self, findings: list[PlanFinding]) -> None:
        """Log one PLAN_VALIDATED event with a per-kind finding count."""
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        self._log(EventType.PLAN_VALIDATED, counts=counts, total=len(findings))

    def install_plan(self, subtasks: list[SubTask]) -> None:
        """Build the DAG + scheduler from a ready plan (bypasses the planner).

        Also accepted from ``COMPLETED`` and ``FAILED`` so a cascade wave can
        be installed immediately after a finished wave without re-initializing.
        Per-wave accumulators are reset so the new run starts clean.
        """
        if self.state not in (
            SessionState.INITIALIZED,
            SessionState.PLANNED,
            SessionState.COMPLETED,
            SessionState.FAILED,
        ):
            raise SessionError(f"cannot install a plan from state {self.state}")
        # Reset per-wave tracking so the new wave starts with a clean slate.
        self._completed = []
        self._failed = []
        self._failure_reasons = {}
        self._failure_history = {}
        self._wave_committed = {}
        self._conflict_rejections = 0
        self._redispatches = 0
        self._concurrency_samples = []
        self._dispatches = 0
        self._context_bytes = 0
        self._starved_dispatches = 0
        # Validate/augment against the code graph (grounds ids, adds missing edges)
        # here — so the CLI's plan() path, the TUI's direct install, cascade waves,
        # and user-edited plans all get validation. Idempotent when plan() already
        # validated the same list.
        subtasks, findings = self._validate_subtasks(subtasks)
        self.last_plan_findings = findings
        if findings:
            self._log_plan_findings(findings)
        subtasks = self._apply_default_agent(subtasks)
        dag = DAG(subtasks)
        self._scheduler = Scheduler(
            dag,
            self._lock_table,
            self._runner(),
            self._registry,
            persist_path=self._mak_dir / "task_graph.json",
            max_concurrent=self._max_concurrent,
        )
        self._progress = {
            t.task_id: SubTaskProgress(t.task_id, list(t.target_nodes))
            for t in subtasks
        }
        self.state = SessionState.PLANNED

    def _apply_default_agent(self, subtasks: list[SubTask]) -> list[SubTask]:
        """Assign a valid agent type to every task before dispatch.

        Three cases, so ``registry.get(agent_type)`` can never raise
        ``UnknownAgentTypeError`` mid-run and multi-provider rosters are actually
        used rather than everything landing on the first agent:

        - **empty** ``agent_type`` → distributed round-robin across the agent pool
          (the healthy configured agent types), so a plan that omits agent types
          spreads work across every provider instead of only the default;
        - **unconfigured/hallucinated** ``agent_type`` (planner named a type that
          is not registered) → remapped to the pool's first entry, with a warning,
          instead of crashing dispatch;
        - **valid** ``agent_type`` → left as-is.

        With no pool (e.g. a direct construction that sets every task's type
        explicitly), tasks are returned unchanged.
        """
        pool = self._agent_pool or (
            [self._default_agent_type] if self._default_agent_type else []
        )
        if not pool:
            return subtasks
        known = set(self._known_agent_types())
        out: list[SubTask] = []
        rr = 0
        for task in subtasks:
            agent_type = task.agent_type
            if not agent_type:
                agent_type = pool[rr % len(pool)]
                rr += 1
            elif known and agent_type not in known:
                self._log(
                    EventType.TASK_COMPLETED,
                    task_id=task.task_id,
                    remapped_agent_type=agent_type,
                    to=pool[0],
                )
                agent_type = pool[0]
            out.append(
                task if agent_type == task.agent_type
                else replace(task, agent_type=agent_type)
            )
        return out

    def _known_agent_types(self) -> list[str]:
        """Agent types the registry can resolve (empty if it can't enumerate)."""
        lister = getattr(self._registry, "list_types", None)
        return list(lister()) if callable(lister) else []

    # -- phase 3: run ------------------------------------------------------

    def run(self, max_iterations: int = 1000) -> SessionResult:
        """Drive the concurrent scheduler loop until done or progress stalls."""
        if self.state is not SessionState.PLANNED:
            raise SessionError(f"cannot run from state {self.state}")
        scheduler = self._require_scheduler()
        self.state = SessionState.RUNNING

        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._run_heartbeat,
            args=(stop,),
            name=f"mak-heartbeat-{self.session_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._run_loop(scheduler, max_iterations)
        finally:
            stop.set()
            heartbeat.join(timeout=self._heartbeat_interval + 1.0)
            self.close()

        return self._finalize(scheduler)

    def _run_loop(self, scheduler: Scheduler, max_iterations: int) -> None:
        """Dispatch concurrently, collect batches, and process them to completion."""
        last_deadlock_scan = time.monotonic()
        for _iteration in range(max_iterations):
            scheduler.tick()
            self._submit_partials()
            # Sample realized parallelism: how many tasks are in flight this tick.
            self._concurrency_samples.append(len(scheduler.dispatched))

            now = time.monotonic()
            if now - last_deadlock_scan >= self._deadlock_interval:
                self._check_deadlocks()
                last_deadlock_scan = now

            if scheduler.is_done():
                break
            if not scheduler.dispatched:
                # Nothing is in flight and the DAG is not done — the remaining
                # tasks are blocked on locks that never freed, or stranded.
                break

            batch = self._collect_batch()
            if not batch:
                # Collection timed out with work in flight: a worker is wedged.
                break
            self._process_batch(batch)

    def _finalize(self, scheduler: Scheduler) -> SessionResult:
        """Compute the terminal state and result after the loop exits."""
        # A task that is neither completed nor explicitly failed was stranded. It
        # must NOT be reported as success — the run is COMPLETED only when the DAG is
        # genuinely done. Split the strays: those with a failed ancestor are *skipped*
        # (an expected downstream consequence), the rest are genuinely *blocked*.
        accounted = set(self._completed) | set(self._failed)
        unaccounted = [
            tid for tid in scheduler.dag.remaining() if tid not in accounted
        ]
        tainted = self._failed_descendants(scheduler.dag.tasks)
        skipped = [tid for tid in unaccounted if tid in tainted]
        blocked = [tid for tid in unaccounted if tid not in tainted]

        if scheduler.is_done() and not self._failed and not blocked and not skipped:
            self.state = SessionState.COMPLETED
        else:
            self.state = SessionState.FAILED
        if skipped or blocked:
            self._log(
                EventType.SESSION_ENDED,
                skipped=skipped,
                blocked=blocked,
                stalled=True,
            )
        metrics = self._plan_metrics()
        self._log(EventType.PLAN_METRICS, **metrics)
        return SessionResult(
            state=self.state,
            completed=tuple(self._completed),
            failed=tuple(self._failed),
            blocked=tuple(blocked),
            skipped=tuple(skipped),
            noop=tuple(self._noop_task_ids()),
            failure_reasons={
                t: self._failure_reasons[t]
                for t in self._failed
                if t in self._failure_reasons
            },
            metrics=metrics,
        )

    def _plan_metrics(self) -> dict[str, float]:
        """Realized-parallelism and rework metrics for the wave just run.

        ``tasks_completed`` counts every task that closed — but a task closes only
        by producing work or by *asserting* there was none, and ``tasks_noop``
        says how many did the latter. Before this split, an empty response on a
        file that happened to exist was counted as a completion indistinguishable
        from real work, so the headline number overstated what a run had done.

        ``context_bytes_total`` / ``mean_context_bytes`` are the input side of the
        same accounting: what the run actually *gave* its agents. A wave whose
        mean is near zero produced its results without being shown the code, which
        is worth knowing before trusting them — and ``starved_dispatches`` counts
        the ones the kernel refused outright.
        """
        samples = self._concurrency_samples
        mean = round(sum(samples) / len(samples), 2) if samples else 0.0
        mean_bytes = (
            round(self._context_bytes / self._dispatches, 2)
            if self._dispatches
            else 0.0
        )
        return {
            "max_concurrency": float(max(samples, default=0)),
            "mean_concurrency": mean,
            "conflict_rejections": float(self._conflict_rejections),
            "redispatches": float(self._redispatches),
            "tasks_completed": float(len(self._completed)),
            "tasks_noop": float(len(self._noop_task_ids())),
            "tasks_failed": float(len(self._failed)),
            "dispatches": float(self._dispatches),
            "context_bytes_total": float(self._context_bytes),
            "mean_context_bytes": mean_bytes,
            "starved_dispatches": float(self._starved_dispatches),
        }

    def _noop_task_ids(self) -> list[str]:
        """Completed tasks where *every* closed grant was an asserted no-op.

        Derived rather than tracked, so a task that changed one node and declined
        another still counts as work done — the distinction only matters when a
        task produced nothing at all.
        """
        noop: list[str] = []
        for task_id in self._completed:
            progress = self._progress.get(task_id)
            if (
                progress is not None
                and progress.noop_nodes
                and progress.completed_nodes <= progress.noop_nodes
            ):
                noop.append(task_id)
        return noop

    def _failed_descendants(self, tasks: dict[str, SubTask]) -> set[str]:
        """Tasks that (transitively) depend on a failed task.

        Iterates to a fixpoint over the dependency edges so a failure propagates the
        whole way down the chain (a task depending on a skipped task is skipped too).
        """
        tainted = set(self._failed)
        changed = True
        while changed:
            changed = False
            for tid, task in tasks.items():
                if tid in tainted:
                    continue
                if any(dep in tainted for dep in task.depends_on):
                    tainted.add(tid)
                    changed = True
        return tainted - set(self._failed)

    # -- collection & batch processing ------------------------------------

    def _collect_batch(self) -> list[_Completion]:
        """Block for the first completion, then drain every result already done.

        Batching is what lets the conflict detector see *cross-agent* edits: all
        results that finished around the same time are validated together.
        """
        try:
            first = self._completions.get(timeout=self._collect_timeout)
        except queue.Empty:
            return []
        batch = [first]
        while True:
            try:
                batch.append(self._completions.get_nowait())
            except queue.Empty:
                break
        return batch

    def _process_batch(self, batch: list[_Completion]) -> None:
        """Validate and commit a batch of results in a deterministic order.

        Tasks are committed in topological order (then by id). Each task is
        validated against the fragments already committed earlier in *this* batch
        (``peers``), so a genuine cross-agent conflict is attributed to the later
        task, which is rejected and retried while the earlier one stands.
        """
        by_id = {c.bundle.task_id: c for c in batch}
        peers: dict[str, str] = {}
        for task_id in self._batch_order(list(by_id)):
            completion = by_id[task_id]
            committed = self._process_one(
                completion.bundle, completion.result, peers
            )
            peers.update(committed)

    def _batch_order(self, task_ids: list[str]) -> list[str]:
        """Order a batch's task ids by topological index, then id (deterministic)."""
        order = self._require_scheduler().dag.topological_order()
        index = {tid: i for i, tid in enumerate(order)}
        return sorted(set(task_ids), key=lambda t: (index.get(t, len(index)), t))

    def _process_result(self, bundle: TaskBundle, result: TaskResult) -> None:
        """Validate, commit, and account a single result (no batch peers)."""
        self._process_one(bundle, result, {})

    def _process_one(
        self, bundle: TaskBundle, result: TaskResult, peers: dict[str, str]
    ) -> dict[str, str]:
        """Validate/commit one result; return the sources it committed (for peers)."""
        task_id = bundle.task_id
        progress = self._progress[task_id]
        progress.attempts += 1
        in_scope = set(progress.target_nodes)
        reported = dict.fromkeys([*result.modified_nodes, *result.new_sources])
        self._log_agent_result(progress, result, reported)
        accepted: list[NodeId] = []
        if result.success:
            accepted = self._stage_returned_sources(
                task_id, progress.target_nodes, result.new_sources
            )
        elif result.error:
            # The agent call itself failed (API error, or a truncated/malformed
            # structured response). Keep the reason so the run can report it.
            self._record_failure(task_id, result.error)
        # A node is committable only if a pending fragment actually exists for it —
        # either staged here from the agent's returned source, or put directly by a
        # test/local runner. An id the agent *claims* it changed but provided no
        # source for cannot be committed (the task stays incomplete and retries);
        # ``_describe_empty_result`` below names that case rather than leaving the
        # operator with a symptom.
        staged = [
            n
            for n in dict.fromkeys([*reported, *accepted])
            if n in in_scope and self._node_store.get_staged(n) is not None
        ]

        committed = (
            self._validate_and_commit(task_id, staged, peers)
            if result.success
            else []
        )
        committed_sources: dict[str, str] = {}
        for node_id in committed:
            progress.completed_nodes.add(node_id)
            self._release_lock(task_id, node_id)
            source = self._node_source(node_id)
            if source is not None:
                committed_sources[str(node_id)] = source

        if self._is_asserted_noop(result):
            self._accept_noop(progress, result)

        # Nothing was stageable and the task is still open: say *why*, now, while
        # the returned ids are still in hand. Left to _handle_incomplete this
        # becomes a catch-all string that names no suspect.
        if result.success and not staged and not progress.is_complete:
            self._record_failure(
                task_id, self._describe_empty_result(progress, result)
            )

        if progress.is_complete:
            self._finish_task(task_id)
        else:
            self._handle_incomplete(progress, result)
        return committed_sources

    @staticmethod
    def _is_asserted_noop(result: TaskResult) -> bool:
        """Whether the agent *claimed* there was nothing to change.

        The old rule was "success with no fragments", which a reply cut off at the
        provider's output cap satisfies exactly — so truncated work was closed as
        a completed task whenever the target file happened to exist already.
        Acceptance now needs positive evidence the agent could only have produced
        by finishing: the flag it was asked to set, and a reply the provider did
        not report as cut short.
        """
        return (
            result.success
            and result.no_changes_required
            and not result.modified_nodes
            and not result.new_sources
        )

    def _accept_noop(self, progress: SubTaskProgress, result: TaskResult) -> None:
        """Close the grants of a task the agent asserted needed no change.

        Still gated on the target existing and its file parsing: an assertion that
        nothing needs changing is not evidence about a file that is missing, or
        one whose syntax error is the very bug the task was sent to fix.
        """
        for node_id in progress.target_nodes:
            if (
                node_id in progress.completed_nodes
                or not self._target_exists(node_id)
                or not self._file_is_syntactically_valid(node_id)
            ):
                continue
            # Sync committed node store content to disk. The on-disk file may
            # pre-date the committed version (e.g. an earlier MAK run wrote a
            # corrected whole-file node but failed to reconstruct because other
            # fragments were still broken).
            try:
                self._reconstruct_affected([node_id])
            except (SyntaxError, OSError):
                pass
            progress.completed_nodes.add(node_id)
            self._release_lock(progress.task_id, node_id)
            progress.noop_nodes.add(node_id)
        if progress.noop_nodes:
            self._log(
                EventType.ACCEPTED_NOOP,
                task_id=progress.task_id,
                attempt=progress.attempts,
                nodes=[str(n) for n in sorted(progress.noop_nodes)],
                reason=result.error or "agent asserted no changes were required",
            )

    def _log_agent_result(
        self,
        progress: SubTaskProgress,
        result: TaskResult,
        reported: dict[NodeId, None],
    ) -> None:
        """Record what the agent actually returned for this attempt.

        Enough to reconstruct a dropped-result failure from the log alone: the
        grant, the ids that came back, how much source came with each, and — the
        field whose absence made a truncation indistinguishable from a deliberate
        no-op — the provider's own stop reason and token usage.
        """
        self._log(
            EventType.AGENT_RESULT,
            task_id=progress.task_id,
            attempt=progress.attempts,
            success=result.success,
            granted=[str(n) for n in progress.target_nodes],
            returned_nodes=[str(n) for n in reported],
            source_lengths={
                str(node_id): len(source)
                for node_id, source in result.new_sources.items()
            },
            no_changes_required=result.no_changes_required,
            stop_reason=result.stop_reason,
            usage=dict(result.usage),
            error=result.error,
        )

    def _describe_empty_result(
        self, progress: SubTaskProgress, result: TaskResult
    ) -> str:
        """Explain why a *successful* agent result left nothing to commit.

        The single catch-all this replaces ("agent reported success but staged no
        usable source") described a symptom shared by four distinct causes, so a
        failed run could not be diagnosed without re-running it.
        """
        granted = ", ".join(str(n) for n in progress.target_nodes)
        reported = dict.fromkeys([*result.modified_nodes, *result.new_sources])
        returned = [str(n) for n in reported]
        if result.stop_reason is not None and matches(
            result.stop_reason, TRUNCATION_STOP_REASONS
        ):
            return (
                "the agent's reply was cut off at the model's output-token limit "
                f"(stop reason: {result.stop_reason}), so no complete source "
                f"arrived (granted: {granted})"
            )
        if returned and result.new_sources:
            return (
                f"agent returned {len(returned)} node id(s), none within its grant "
                f"(granted: {granted}; returned: {', '.join(returned)})"
            )
        if returned:
            return (
                f"agent listed {len(returned)} modified node(s) but returned no "
                f"source for any of them (returned: {', '.join(returned)})"
            )
        missing = [n for n in progress.remaining if not self._target_exists(n)]
        if missing:
            return (
                "agent returned success with no sources and the target does not "
                f"exist (missing: {', '.join(str(n) for n in missing)})"
            )
        invalid = [
            n for n in progress.remaining if not self._file_is_syntactically_valid(n)
        ]
        if invalid:
            return (
                "agent returned success with no changes, but the target file is "
                f"still not valid Python ({', '.join(str(n) for n in invalid)})"
            )
        return (
            "agent returned success with no sources and did not assert that no "
            f"change was required (granted: {granted}); an empty reply is not "
            "evidence the task was done — it is also what a reply cut off at the "
            "output-token limit looks like"
        )

    def _target_exists(self, node_id: NodeId) -> bool:
        """Whether a target already exists committed (so a no-op leaves it intact).

        True if the node itself is committed, or — for a whole-file target (a bare
        ``path.py``) — if the file already has committed fragments from ingestion.
        In both cases the file must also exist on disk. If the node is committed but
        the file has been deleted (stale node store from a prior session), it is
        reconstructed from committed fragments before returning True.
        """
        file_path = str(node_id).split("::", 1)[0]
        in_store = self._node_source(node_id) is not None or (
            "::" not in str(node_id)
            and bool(self._node_store.get_committed_fragments(str(node_id)))
        )
        if not in_store:
            return False
        if (self._work_dir / file_path).exists():
            return True
        # Node is committed but file is missing from disk — reconstruct it so
        # the no-op acceptance does not silently leave the filesystem inconsistent.
        fragments = self._node_store.get_committed_fragments(file_path)
        if not fragments:
            return False
        try:
            reconstruct_file(fragments, output_path=self._work_dir / file_path)
            return True
        except (SyntaxError, OSError):
            return False

    def _validate_and_commit(
        self, task_id: str, staged: list[NodeId], peers: dict[str, str] | None = None
    ) -> list[NodeId]:
        """Validate, then transactionally commit staged fragments.

        Order matters: conflict detection (against this batch's already-committed
        peers) → *prospective* reconstruction validated against ``compile()`` →
        commit-time lock re-validation → commit → write files. The store is only
        advanced once the would-be file is valid Python and we still own every
        write lock, and a write failure after commit reverts the commit so disk
        and store never diverge.
        """
        if not staged:
            return []
        report = self._conflict_detector.detect(
            self._build_edit_round(staged, peers or {})
        )
        if not report.ok:
            self._reject(task_id, staged, report.reasons)
            return []
        if not self._preview_is_valid(staged):
            self._reject(
                task_id, staged, ["reconstruction would produce invalid Python"]
            )
            return []
        # RA-3: a lease may have expired during a long agent call (and the node
        # reclaimed by another holder). Confirm we still own every write lock
        # before advancing the store, so we never commit through a stolen lock.
        if not self._lock_table.holds_all(
            [(node_id, LockMode.WRITE) for node_id in staged], task_id
        ):
            self._reject(
                task_id, staged, ["write lock lost before commit (lease expired)"]
            )
            return []

        committed: list[NodeId] = []
        for node_id in staged:
            old_source = self._node_source(node_id)  # snapshot before commit
            self._node_store.commit_node(node_id)
            committed.append(node_id)
            new_source = self._node_source(node_id)  # snapshot after commit
            if new_source is not None:
                self._wave_committed[node_id] = (old_source, new_source)
        try:
            self._reconstruct_affected(staged)
        except (SyntaxError, OSError) as exc:
            # The store advanced but the file did not — undo the commits so disk
            # and store stay consistent, and fail the task.
            self._revert(committed)
            self._log(
                EventType.CONFLICT_DETECTED,
                task_id=task_id,
                reasons=[f"reconstruction failed after commit: {exc}"],
            )
            return []
        self._audit_commit(task_id, staged)
        return committed

    def _reject(self, task_id: str, staged: list[NodeId], reasons: list[str]) -> None:
        """Log a rejection and discard the staged (pending) fragments."""
        self._conflict_rejections += 1
        self._log(EventType.CONFLICT_DETECTED, task_id=task_id, reasons=reasons)
        if reasons:
            self._record_failure(task_id, "; ".join(reasons))
        for node_id in staged:
            self._node_store.rollback_node(node_id)

    def _revert(self, committed: list[NodeId]) -> None:
        """Best-effort roll committed nodes back to their previous version."""
        for node_id in committed:
            try:
                self._node_store.revert_node(node_id)
            except NodeStoreError:
                # A brand-new node has no prior version to revert to (documented
                # limitation); leave it and let the loud log surface the desync.
                continue

    def _preview_is_valid(self, staged: list[NodeId]) -> bool:
        """Assemble each affected file with staged versions and check it parses."""
        staged_set = set(staged)
        files = sorted({str(n).split("::", 1)[0] for n in staged})
        for file_path in files:
            try:
                src = self._assemble_preview(file_path, staged_set)
                compile(src, "<mak-preview>", "exec")
            except SyntaxError:
                return False
        return True

    def _assemble_preview(self, file_path: str, staged_set: set[NodeId]) -> str:
        """Build a file's prospective source: committed fragments + staged swaps.

        Delegates to ``NodeStore.get_preview_fragments`` so that fragments are
        re-indented (class methods back to column 4, etc.) before assembly —
        the same transformation ``get_committed_fragments`` applies during real
        reconstruction.  Using dedented ``get_node()`` sources here would make
        any file with class methods fail ``ast.parse`` unconditionally.
        """
        staged_overrides = {
            node_id: frag
            for node_id in staged_set
            if (frag := self._node_store.get_staged(node_id)) is not None
        }
        return assemble_fragments(
            self._node_store.get_preview_fragments(file_path, staged_overrides)
        )

    def _build_edit_round(
        self, staged: list[NodeId], peers: dict[str, str] | None = None
    ) -> EditRound:
        """Assemble an EditRound from staged fragments plus this batch's peers.

        ``definitions`` spans every staged source in the batch (this task's plus
        the peers already committed), so a signature change anywhere is the
        authority for this task's call sites — the cross-agent signature check.
        ``symbol_edits`` / ``header_edits`` are scoped to the *files this task
        touches*: name collisions and import conflicts are file-local, so feeding
        unrelated files would only invent false positives.
        """
        peers = peers or {}
        own: dict[str, str] = {}
        for node_id in staged:
            fragment = self._node_store.get_staged(node_id)
            if fragment is not None:
                own[str(node_id)] = fragment.source
        own_files = {_file_of(k) for k in own}
        definitions = {**peers, **own}
        same_file = {
            k: v for k, v in definitions.items() if _file_of(k) in own_files
        }
        headers = {k: v for k, v in same_file.items() if _is_header_id(k)}
        # Each staged source is both a definition authority and a caller, so the
        # detector validates this task's new calls against every new signature.
        return EditRound(
            definitions=definitions,
            callers=own,
            header_edits=headers,
            symbol_edits=same_file,
        )

    def _reconstruct_affected(self, nodes: list[NodeId]) -> list[str]:
        """Rewrite each file touched by ``nodes`` from its committed fragments."""
        files = sorted({str(n).split("::", 1)[0] for n in nodes})
        for file_path in files:
            fragments = self._node_store.get_committed_fragments(file_path)
            if not fragments:
                # A committed node that yields no fragments would leave nothing on
                # disk yet still be reported as written — fail loudly (the caller
                # reverts the commit) instead of failing later at `git add`.
                raise OSError(
                    f"no committed fragments for '{file_path}'; nothing to write"
                )
            reconstruct_file(fragments, output_path=self._work_dir / file_path)
        return files

    def _audit_commit(self, task_id: str, nodes: list[NodeId]) -> None:
        """Record a git audit commit for the task's files, if git is enabled."""
        if self._git is None or not self._config.git.auto_commit:
            return
        files = sorted({str(n).split("::", 1)[0] for n in nodes})
        task = self._dag_task(task_id)
        self._git.commit_task(
            task_id=task_id,
            files=files,
            description=task.description,
            agent_type=task.agent_type or "unknown",
            session_id=self.session_id,
        )

    def _finish_task(self, task_id: str) -> None:
        scheduler = self._require_scheduler()
        scheduler.on_task_complete(task_id)
        self._completed.append(task_id)
        self._log(EventType.TASK_COMPLETED, task_id=task_id)

    def _handle_incomplete(
        self, progress: SubTaskProgress, result: TaskResult | None = None
    ) -> None:
        """Retry remaining grants, or fail the task once attempts are exhausted.

        A retry is only worth an attempt if it can differ from the one that
        failed. Two cases where it cannot:

        - the provider *refused* — the same prompt earns the same refusal, so the
          task fails now rather than after three identical calls;
        - nothing at all was learned — impossible here, since every path that
          reaches this point has recorded a failure reason, which is fed back to
          the agent as ``retry_note`` on the re-dispatch.
        """
        scheduler = self._require_scheduler()
        exhausted = progress.attempts >= self._max_attempts
        unretryable = result is not None and not result.retryable
        if exhausted or unretryable:
            scheduler.on_task_failed(progress.task_id, requeue=False)
            self._failed.append(progress.task_id)
            reason = self._final_failure_reason(progress)
            self._failure_reasons[progress.task_id] = reason
            if unretryable and not exhausted:
                reason = (
                    f"{reason} (not retryable — the remaining "
                    f"{self._max_attempts - progress.attempts} attempt(s) would "
                    "repeat it verbatim)"
                )
                self._failure_reasons[progress.task_id] = reason
            self._log(
                EventType.TASK_COMPLETED,
                task_id=progress.task_id,
                failed=True,
                reason=reason,
            )
        else:
            # Remaining nodes are still locked from the original acquisition; queue
            # a narrowed re-dispatch covering only what is left, carrying why the
            # last attempt produced nothing.
            progress.retry_note = self._retry_note(progress, result)
            self._redispatches += 1
            self._partial_queue.append(progress.task_id)

    def _record_failure(self, task_id: str, reason: str) -> None:
        """Record why an attempt made no progress, keeping the earlier ones.

        ``_failure_reasons`` holds the latest reason (what the retry acts on);
        ``_failure_history`` accumulates the distinct ones so the *final* report
        can name a cause that recurred across attempts rather than whichever
        happened to land last.
        """
        self._failure_reasons[task_id] = reason
        history = self._failure_history.setdefault(task_id, [])
        if reason not in history:
            history.append(reason)

    def _final_failure_reason(self, progress: SubTaskProgress) -> str:
        """Summarize why a task failed across *all* of its attempts.

        A task can fail differently each time, and reporting only the last
        attempt buries the cause: one real run rejected attempts 1-2 on the same
        underlying defect and then hit a one-off malformed response on attempt 3
        — so the run reported only the malformed response, which named nothing
        relevant to the actual problem. Distinct reasons are therefore all
        reported, in the order they were first seen.
        """
        history = self._failure_history.get(progress.task_id, [])
        if not history:
            return (
                "agent reported success but staged no usable source "
                f"after {progress.attempts} attempt(s)"
            )
        if len(history) == 1:
            return history[0]
        listed = "; ".join(f"({i}) {r}" for i, r in enumerate(history, 1))
        return (
            f"{progress.attempts} attempts failed for {len(history)} "
            f"reasons: {listed}"
        )

    def _retry_note(
        self, progress: SubTaskProgress, result: TaskResult | None
    ) -> str | None:
        """Return the instruction to attach to the next attempt at this task.

        A retry is only worth an attempt if it can differ from the one that
        failed, and *what* to change depends on how it failed:

        - a **truncation** gets a compaction instruction rather than the generic
          "that failed, try again": re-sending an identical request produces an
          identically-cut reply, which is exactly how one task burned three
          attempts on three byte-identical failures;
        - a **schema slip** gets the schema restated. The generic note says the
          previous answer was unusable but never what shape was wanted, so a run
          that returned ``modified_fragments`` as a string returned it as a
          string three times — ~18k output tokens, one failed task, and twelve
          dependents stranded behind it.
        """
        reason = self._failure_reasons.get(progress.task_id)
        truncated = result is not None and matches(
            result.stop_reason, TRUNCATION_STOP_REASONS
        )
        if not truncated and result is not None and result.error_kind == "protocol":
            return (
                f"Your previous response did not match the result schema: {reason}. "
                "'modified_fragments' must be a JSON array of objects, each "
                '{"node_id": "<an id copied verbatim from target_nodes>", '
                '"new_source": "<the node\'s complete new source>"}. Emit it as '
                "structured tool input — not as a string, and not as a "
                "JSON-encoded array inside a string. If you have nothing to "
                "return, set no_changes_required instead of sending an empty or "
                "differently-shaped field."
            )
        if truncated:
            return (
                "Your previous response was cut off at the model's output-token "
                "limit before the result was complete, so none of it could be "
                "used. Return the same work in less output: emit only the nodes "
                "you actually changed, no commentary, and no unchanged code. If "
                "one node's full source genuinely cannot fit in a single "
                "response, return success=false with an error saying so rather "
                "than a partial rewrite."
            )
        if reason is None:
            return None
        return (
            f"Your previous attempt at this task produced nothing usable: {reason}. "
            "Do not repeat it — return the full source of every node you change, "
            "under the exact node ids in target_nodes."
        )

    def _submit_partials(self) -> None:
        """Re-dispatch the narrowed remaining grants of each partial task (async)."""
        if not self._partial_queue:
            return
        queued, self._partial_queue = self._partial_queue, []
        runner = self._runner()
        for task_id in queued:
            progress = self._progress[task_id]
            task = self._dag_task(task_id)
            adapter = self._registry.get(task.agent_type)
            bundle = TaskBundle(
                task_id=task_id,
                description=task.description,
                target_nodes=progress.remaining,
                retry_note=progress.retry_note,
            )
            runner.assign(adapter, bundle)

    def _enrich_bundle(self, bundle: TaskBundle) -> _Dispatch:
        """Attach every layer of context, record what was attached, and gate it.

        Five layers, each only adding entries not already present:

        1. ``write_source:<id>`` — every node the agent will modify.
        2. ``read_source:<id>`` — nodes the planner explicitly listed as context.
        3. ``read_source:<id>`` — all other nodes in the same file as any write
           target.  Gives the agent full sight of imports, siblings, and class
           structure without relying on the planner.
        4. ``read_source:<id>`` — nodes in *other* files whose source contains
           any target symbol name (word-boundary match).  Captures cross-file
           callers and callees so the agent is never blind to dependencies that
           live outside its own file.  Bounded and quality-filtered — see
           ``_add_cross_file_references``.
        5. ``read_source:<id>`` / ``read_api:<id>`` — the committed output of the
           tasks this one directly ``depends_on``.  Layers 1-4 all derive from
           code that already exists, so for a task whose targets are brand-new
           files they return *nothing*; this is the layer that carries what a
           dependency built.

        Each layer reports the context keys it added, so ``TASK_DISPATCHED`` can
        say *which layer* put a node in the bundle. Counts alone were not enough:
        attributing one real run's 151 KB bundle meant re-deriving the layers by
        hand from ``task_graph.json`` and the source tree.

        The result is logged (``TASK_DISPATCHED``) and, when it is empty for a
        task that declares dependencies, refused rather than sent — see
        :class:`_Dispatch`.
        """
        task = self._dag_task(bundle.task_id)
        context = dict(bundle.context)
        target_files = {str(n).split("::", 1)[0] for n in bundle.target_nodes}
        layers: dict[str, list[str]] = {}
        layers["write_targets"] = self._add_write_targets(
            bundle.target_nodes, context
        )
        layers["planner_context"] = self._add_planner_context(
            task.context_nodes, context
        )
        layers["same_file"] = self._add_same_file_siblings(
            bundle.target_nodes, context
        )
        layers["cross_file"], dropped = self._add_cross_file_references(
            bundle.target_nodes, target_files, context
        )
        layers["dependency_output"] = self._add_dependency_outputs(task, context)
        return self._gate_dispatch(
            task,
            replace(bundle, context=context),
            layers,
            cross_file_dropped=dropped,
        )

    def _add_write_targets(
        self, target_nodes: list[NodeId], context: dict[str, str]
    ) -> list[str]:
        """Layer 1: the current source of every node the agent will modify."""
        added: list[str] = []
        for node_id in target_nodes:
            source = self._node_source(node_id)
            if source is not None:
                key = f"write_source:{node_id}"
                context[key] = source
                added.append(key)
        return added

    def _add_planner_context(
        self, context_nodes: list[NodeId], context: dict[str, str]
    ) -> list[str]:
        """Layer 2: the nodes the planner explicitly listed as context."""
        added: list[str] = []
        for node_id in context_nodes:
            source = self._node_source(node_id)
            if source is not None:
                key = f"read_source:{node_id}"
                context[key] = source
                added.append(key)
        return added

    def _add_same_file_siblings(
        self, target_nodes: list[NodeId], context: dict[str, str]
    ) -> list[str]:
        """Layer 3: every other committed node in a write target's own file."""
        added: list[str] = []
        for node_id in target_nodes:
            file_path = str(node_id).split("::", 1)[0]
            for sibling_id in self._node_store.list_nodes(file_path):
                if _context_has(context, sibling_id):
                    continue
                source = self._node_source(sibling_id)
                if source is not None:
                    key = f"read_source:{sibling_id}"
                    context[key] = source
                    added.append(key)
        return added

    def _add_cross_file_references(
        self,
        target_nodes: list[NodeId],
        target_files: set[str],
        context: dict[str, str],
    ) -> tuple[list[str], int]:
        """Layer 4: nodes in other files that mention a target symbol by name.

        Returns ``(keys added, nodes dropped for budget)``.

        This was the most expensive layer in a real bundle and had no ceiling at
        all: one task carried 151 KB here (67,847 input tokens), of which 88%
        matched on nothing but ``__all__``. Three things now keep it honest, in a
        single pass over the store:

        - a symbol shorter than ``_MIN_SYMBOL_LEN`` is a word, not evidence of a
          relationship — ``run`` matched six unrelated files in that run;
        - a symbol matching more than ``_MAX_SYMBOL_MATCHES`` nodes is not evidence
          either, and is discarded wholesale rather than node by node;
        - what survives is ranked by match count (most first, then smallest, then
          id) and added until ``session.cross_file_context_bytes`` is spent.

        Past the budget an entry is **dropped**, not degraded to a digest as layer 5
        does: a caller's value *is* its call site, and a signature digest of a caller
        says nothing about how it calls.
        """
        budget = self._config.session.cross_file_context_bytes
        symbols = {
            s for s in self._target_symbols(target_nodes)
            if len(s) >= _MIN_SYMBOL_LEN
        }
        if not symbols or budget == 0:
            return [], 0
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(s) for s in sorted(symbols)) + r")\b"
        )
        candidates = self._scan_for_symbols(pattern, target_files, context)
        return self._spend_cross_file_budget(candidates, context, budget)

    def _scan_for_symbols(
        self,
        pattern: re.Pattern[str],
        target_files: set[str],
        context: dict[str, str],
    ) -> list[tuple[NodeId, str, frozenset[str]]]:
        """One pass over the store: each matching node with the symbols it hit.

        ``findall`` rather than ``search`` because the symbols a node matched are
        what decides both whether that symbol is over-broad and how the node ranks.
        """
        found: list[tuple[NodeId, str, frozenset[str]]] = []
        for xfile_id in self._node_store.list_nodes():
            if str(xfile_id).split("::", 1)[0] in target_files:
                continue  # same-file already handled in layer 3
            if _context_has(context, xfile_id):
                continue
            source = self._node_source(xfile_id)
            if not source:
                continue
            hits = frozenset(pattern.findall(source))
            if hits:
                found.append((xfile_id, source, hits))
        return found

    @staticmethod
    def _spend_cross_file_budget(
        candidates: list[tuple[NodeId, str, frozenset[str]]],
        context: dict[str, str],
        budget: int,
    ) -> tuple[list[str], int]:
        """Discard over-broad symbols, rank what is left, and fill the budget."""
        counts: Counter[str] = Counter()
        for _node_id, _source, hits in candidates:
            counts.update(hits)
        broad = {s for s, n in counts.items() if n > _MAX_SYMBOL_MATCHES}
        kept = [
            (node_id, source, hits - broad)
            for node_id, source, hits in candidates
            if hits - broad
        ]
        kept.sort(key=lambda c: (-len(c[2]), len(c[1]), str(c[0])))
        added: list[str] = []
        dropped = 0
        spent = 0
        for node_id, source, _hits in kept:
            if budget >= 0 and spent + len(source) > budget:
                dropped += 1
                continue  # a smaller node further down may still fit
            key = f"read_source:{node_id}"
            context[key] = source
            added.append(key)
            spent += len(source)
        return added, dropped

    def _target_symbols(self, target_nodes: list[NodeId]) -> set[str]:
        """Return the symbol names a task's targets define, for the layer-4 scan.

        A ``file::kind::name`` id contributes the rightmost segment of its
        qualified name ("apple" from "FruitManager.apple"). A bare-path
        *whole-file* id has no name segment at all — and whole-file grants are not
        an edge case, Wave 11's folding made them the normal shape — so an id like
        ``editor/home.py`` used to contribute nothing and silently disable the
        entire layer. Its symbols come from the file's committed nodes instead.
        """
        symbols: set[str] = set()
        for node_id in target_nodes:
            parts = str(node_id).split("::")
            if len(parts) >= 3:
                symbols.add(_symbol_of(parts[2]))
            else:
                symbols |= self._file_symbols(parts[0])
        return {s for s in symbols if s}

    def _file_symbols(self, file_path: str) -> set[str]:
        """Symbol names defined by a file's committed nodes, fragments or whole."""
        symbols: set[str] = set()
        for node_id in self._node_store.list_nodes(file_path):
            parts = str(node_id).split("::")
            if len(parts) >= 3:
                symbols.add(_symbol_of(parts[2]))
        source = self._node_source(NodeId(file_path))
        if source is not None:
            symbols |= _defined_symbol_names(source)
        return symbols

    def _add_dependency_outputs(
        self, task: SubTask, context: dict[str, str]
    ) -> list[str]:
        """Layer 5: the committed output of every task this one depends on.

        ``depends_on`` is MAK's own assertion that the earlier task's output
        matters to the later one, and by dispatch time the DAG guarantees that
        output is committed and readable — yet nothing looked at it. A task whose
        dependencies created new files therefore arrived with an empty bundle and
        invented their APIs; one such guess shipped a call that raises
        ``TypeError`` the first time it runs.

        Direct dependencies only: the transitive closure grows without bound.
        Spending is bounded by ``session.dependency_context_bytes`` — past the
        budget an entry degrades to a public API digest rather than being dropped,
        because a test-writing task needs its dependency's *contract*, not its
        bodies, and "informed cheaply" beats "blind". Returns the keys it added.
        """
        budget = self._config.session.dependency_context_bytes
        if budget == 0:
            return []
        added: list[str] = []
        spent = 0
        for dep_id in sorted(task.depends_on):
            for node_id in self._dag_task(dep_id).target_nodes:
                if _context_has(context, node_id):
                    continue
                source = self._dependency_source(node_id)
                if not source:
                    continue
                if budget < 0 or spent + len(source) <= budget:
                    key = f"read_source:{node_id}"
                    context[key] = source
                    added.append(key)
                    spent += len(source)
                    continue
                digest = public_api_digest(source)
                if digest:
                    key = f"read_api:{node_id}"
                    context[key] = digest
                    added.append(key)
                    spent += len(digest)
        return added

    def _dependency_source(self, node_id: NodeId) -> str | None:
        """Return a dependency target's source, assembling a file when needed.

        A whole-file target is often committed as fragments rather than as one
        bare-path node, so the bare id itself has no source of its own.
        """
        source = self._node_source(node_id)
        if source is not None:
            return source
        if "::" in str(node_id):
            return None
        fragments = self._node_store.get_committed_fragments(str(node_id))
        return assemble_fragments(fragments) if fragments else None

    def _gate_dispatch(
        self,
        task: SubTask,
        bundle: TaskBundle,
        layers: dict[str, list[str]],
        *,
        cross_file_dropped: int,
    ) -> _Dispatch:
        """Log what the bundle carries; refuse to dispatch a starved bundle.

        Nothing in the kernel used to notice an empty bundle — no event, no
        metric, no guard — so the only report of it came from the one agent that
        refused to work blind. The others guessed and were recorded as completed.

        ``layers`` carries the per-layer attribution: which layer contributed which
        nodes, and how many bytes each cost. Counts alone left the expensive layer
        unidentifiable without re-deriving it by hand against the plan and the
        source tree.
        """
        counts = _context_counts(bundle.context)
        total_bytes = sum(len(v) for v in bundle.context.values())
        progress = self._progress.get(bundle.task_id)
        starved = not bundle.context and bool(task.depends_on or task.context_nodes)
        self._dispatches += 1
        self._context_bytes += total_bytes
        if starved:
            self._starved_dispatches += 1
        self._log(
            EventType.TASK_DISPATCHED,
            task_id=bundle.task_id,
            attempt=(progress.attempts + 1) if progress is not None else 1,
            targets=[str(n) for n in bundle.target_nodes],
            depends_on=list(task.depends_on),
            write_sources=counts["write_source"],
            read_sources=counts["read_source"],
            read_apis=counts["read_api"],
            context_bytes=total_bytes,
            starved=starved,
            layers=_layer_report(layers, bundle.context),
            cross_file_dropped=cross_file_dropped,
        )
        if not starved:
            return _Dispatch(bundle)
        return _Dispatch(bundle, starved_reason=(
            "kernel defect: the bundle carried no context at all, while the task "
            f"declares {len(task.depends_on)} dependency edge(s) and "
            f"{len(task.context_nodes)} context node(s). The agent would have had "
            "to invent the APIs it was asked to build against."
        ))

    def _stage_returned_sources(
        self, task_id: str, grant: list[NodeId], new_sources: dict[NodeId, str]
    ) -> list[NodeId]:
        """Stage each rewritten source the agent returned; return the ids staged.

        This is the agent→store transport: an API/CLI agent reports the full new
        source of each node it changed, and the session ``put_node``s it (as a new
        pending version) so the normal validate→commit path applies it.

        An agent may not edit beyond the nodes it was authorized to modify, so a
        source outside the grant is refused — but *loudly*: every refusal is
        logged with the id, the grant, and the reason. This transport used to drop
        such ids with a bare ``continue``, which turned a granularity mismatch
        into three identical "staged no usable source" retries and a failed task
        whose real cause was unrecoverable from the log.
        """
        accepted, dropped = map_returned_sources(grant, new_sources)
        for node_id, source in accepted.items():
            self._node_store.put_node(
                node_id, NodeFragment(node_id, self._node_kind(node_id), source, 1)
            )
        for node_id, reason in dropped:
            self._log(
                EventType.SOURCE_DROPPED,
                task_id=task_id,
                node_id=str(node_id),
                granted=[str(n) for n in grant],
                source_length=len(new_sources[node_id]),
                reason=reason,
            )
        return list(accepted)

    def _node_kind(self, node_id: NodeId) -> str:
        """Return a node's stored kind, inferring a sensible kind for a new node.

        A bare-path ``.py`` id (no ``::kind::name``) is a *whole-file* node — the
        agent returned an entire new file as one node — so its kind is ``module``;
        any other new id defaults to ``function``.
        """
        try:
            return self._node_store.get_node(node_id).kind
        except NodeStoreError:
            return "module" if "::" not in str(node_id) else "function"

    def _node_source(self, node_id: NodeId) -> str | None:
        """Return a node's current committed source, or None if it does not exist."""
        try:
            return self._node_store.get_node(node_id).source
        except NodeStoreError:
            return None

    def _file_is_syntactically_valid(self, node_id: NodeId) -> bool:
        """Return True if the committed file containing this node parses as Python.

        Guards the no-op acceptance path: a task whose agent returned success
        with no changes must not be accepted as complete when the file it was
        supposed to fix still has a syntax error.
        """
        file_path = str(node_id).split("::", 1)[0]
        try:
            compile(self._assemble_preview(file_path, set()), "<mak>", "exec")
            return True
        except SyntaxError:
            return False

    def _release_lock(self, task_id: str, node_id: NodeId) -> None:
        self._lock_table.release(node_id, LockMode.WRITE, task_id)

    def _dag_task(self, task_id: str) -> SubTask:
        return self._require_scheduler().dag.get_task(task_id)

    # -- heartbeat & deadlock watchdog ------------------------------------

    def _run_heartbeat(self, stop: threading.Event) -> None:
        """Renew in-flight tasks' leases until ``stop`` is set (RA-3).

        A long agent call must not let its lease lapse and get its lock stolen.
        While the run loop is active, every in-flight holder's leases are renewed
        each interval so a slow-but-alive agent keeps its grants.
        """
        while not stop.wait(self._heartbeat_interval):
            scheduler = self._scheduler
            if scheduler is None:
                continue
            for task_id in scheduler.dispatched:
                self._lock_table.renew_all(task_id)

    def _check_deadlocks(self) -> None:
        """Scan the wait graph for cycles and resolve any via wound-wait.

        With atomic lock pre-allocation a waiting task holds *no* locks, so the
        wait graph can never contain a cycle — this watchdog is defense in depth.
        Should a cycle ever arise (e.g. a future intent-write phase), the youngest
        task in it is aborted and re-queued.
        """
        scheduler = self._scheduler
        if scheduler is None:
            return
        waiting = [
            (task.task_id, node_id, LockMode.WRITE)
            for task in scheduler.ready_queue
            for node_id in task.target_nodes
        ]
        if not waiting:
            return
        held: dict[NodeId, list[tuple[str, LockMode]]] = {}
        start_times: dict[str, float] = {}
        for node_id, entries in self._lock_table.all_entries().items():
            for entry in entries:
                held.setdefault(node_id, []).append((entry.holder, entry.mode))
                prior = start_times.get(entry.holder)
                start_times[entry.holder] = (
                    entry.acquired_at
                    if prior is None
                    else min(prior, entry.acquired_at)
                )
        graph = self._deadlock_detector.build_wait_graph(held, waiting)
        for cycle in self._deadlock_detector.find_cycles(graph):
            victim = self._deadlock_detector.resolve(cycle, start_times)
            scheduler.on_task_failed(victim, requeue=True)
            self._log(
                EventType.CONFLICT_DETECTED, deadlock=list(cycle), aborted=victim
            )

    # -- phase 4: teardown -------------------------------------------------

    def teardown(self) -> bool:
        """Run the test suite; push if green and auto_push is enabled."""
        passed = True
        output = ""
        if self._test_runner is not None:
            passed, output = self._test_runner()
        if passed and self._config.git.auto_push and self._git is not None:
            self._git.push()
        self._log(
            EventType.SESSION_ENDED,
            tests_passed=passed,
            completed=len(self._completed),
            failed=len(self._failed),
            output=output[:500],
        )
        return passed

    # -- crash recovery ----------------------------------------------------

    def recover(self) -> int:
        """Expire stale leases and re-queue incomplete tasks from disk.

        Returns the number of leases expired. Must be called before ``run`` when
        resuming a crashed session; rebuilds the scheduler from ``task_graph.json``
        if one is present.
        """
        expired = self._lock_table.expire_stale()
        graph_path = self._mak_dir / "task_graph.json"
        if graph_path.exists():
            scheduler = Scheduler.from_persisted(
                graph_path,
                self._lock_table,
                self._runner(),
                self._registry,
                max_concurrent=self._max_concurrent,
            )
            self._scheduler = scheduler
            self._progress = {
                t.task_id: self._restore_progress(scheduler, t)
                for t in scheduler.dag.tasks.values()
            }
            self.state = SessionState.PLANNED
        return len(expired)

    @staticmethod
    def _restore_progress(scheduler: Scheduler, task: SubTask) -> SubTaskProgress:
        progress = SubTaskProgress(task.task_id, list(task.target_nodes))
        if scheduler.dag.is_complete(task.task_id):
            progress.completed_nodes = set(task.target_nodes)
        return progress

    # -- cascade detection -------------------------------------------------

    def detect_cascade_tasks(self) -> list[SubTask]:
        """Return fix-up tasks for callers broken by signature changes this wave.

        After ``run()`` completes, this method compares the old and new AST
        signature of every node committed during the wave.  When a function's
        signature changed (parameters or return annotation differ), every node
        in the store — across all files — that references that symbol by name is
        a potential broken caller and gets its own SubTask.

        It also carries the *cross-module* check
        (:meth:`detect_cross_module_defects`): a wave that created two modules
        which disagree about each other's API changed no existing signature, so
        the comparison above sees nothing, yet the code is broken exactly as if it
        had. Both classes of breakage are fix-up work for the next wave, so they
        share one entry point rather than needing a second review flow.

        Returns an empty list when no signatures changed, which is the expected
        outcome when the planner was thorough about including all affected nodes.
        A non-empty return is a signal that the planner missed callers; the
        caller (``__main__``) should present these tasks to the user for review
        before running a second wave.
        """
        tasks = self._cross_module_fix_tasks(self.detect_cross_module_defects())
        changed: list[tuple[NodeId, str, str, str]] = []
        for node_id, (old_src, new_src) in self._wave_committed.items():
            parts = str(node_id).split("::")
            if len(parts) < 3:
                continue
            old_sig = _extract_sig(old_src) if old_src is not None else None
            new_sig = _extract_sig(new_src)
            # Only cascade when an *existing* function's signature changed.
            # New functions (old_src is None) have no prior callers to break.
            if old_sig is not None and new_sig is not None and old_sig != new_sig:
                symbol = parts[2].rsplit(".", 1)[-1]
                changed.append((node_id, symbol, old_sig, new_sig))

        if not changed:
            return tasks

        already_targeted: set[NodeId] = set()

        for node_id, symbol, old_sig, new_sig in changed:
            func_file = str(node_id).split("::", 1)[0]
            pat = re.compile(r"\b" + re.escape(symbol) + r"\b")
            for xfile_id in self._node_store.list_nodes():
                if str(xfile_id).split("::", 1)[0] == func_file:
                    continue  # same-file callers should have been in the plan
                if xfile_id in already_targeted:
                    continue
                source = self._node_source(xfile_id)
                if not (source and pat.search(source)):
                    continue
                already_targeted.add(xfile_id)
                safe_id = re.sub(r"[^a-zA-Z0-9]", "_", f"cascade_{symbol}_{xfile_id}")
                tasks.append(SubTask(
                    task_id=safe_id,
                    description=(
                        f"Update call sites of `{symbol}` in `{xfile_id}` — "
                        f"its signature changed from `{old_sig}` to `{new_sig}`. "
                        "Adjust every call in this node to match the new signature."
                    ),
                    target_nodes=[xfile_id],
                    context_nodes=[node_id],
                    depends_on=[],
                    agent_type=self._default_agent_type or "",
                ))

        return tasks

    def detect_cross_module_defects(self) -> list[CrossModuleDefect]:
        """Report where the files this wave wrote contradict the modules they use.

        Every gate MAK runs is scoped to one task's edit, so two tasks can each
        finish clean and still leave the codebase broken between them: a module
        that imports a name its target never defines, or calls a sibling's
        function with the wrong arity. Both parse, so the parse gate passes; the
        signature is *new*, so cascade detection has no "before" to compare.

        Scope is every file with a node committed this wave, judged against the
        store as it now stands. Each defect is logged as ``CONFLICT_DETECTED`` so
        it is on the record even if the operator declines the fix-up wave.
        """
        scope = frozenset(_file_of(str(n)) for n in self._wave_committed)
        if not scope:
            return []
        defects = check_cross_module_api(self._file_sources(), scope)
        for defect in defects:
            self._log(
                EventType.CONFLICT_DETECTED,
                kind=defect.kind,
                file=defect.file,
                defining_file=defect.defining_file,
                reasons=[defect.detail],
            )
        return defects

    def _file_sources(self) -> dict[str, str]:
        """Return the assembled current source of every file the store holds."""
        sources: dict[str, str] = {}
        paths = {_file_of(str(n)) for n in self._node_store.list_nodes()}
        for file_path in sorted(paths):
            fragments = self._node_store.get_committed_fragments(file_path)
            if fragments:
                sources[file_path] = assemble_fragments(fragments)
        return sources

    def _cross_module_fix_tasks(
        self, defects: list[CrossModuleDefect]
    ) -> list[SubTask]:
        """One fix-up task per file whose cross-module references do not resolve."""
        by_file: dict[str, list[CrossModuleDefect]] = {}
        for defect in defects:
            by_file.setdefault(defect.file, []).append(defect)
        tasks: list[SubTask] = []
        for file_path, found in sorted(by_file.items()):
            listed = "; ".join(d.detail for d in found)
            defining = sorted({d.defining_file for d in found})
            tasks.append(SubTask(
                task_id=re.sub(r"[^a-zA-Z0-9]", "_", f"api_fix_{file_path}"),
                description=(
                    f"Fix `{file_path}` so its use of "
                    f"{', '.join(f'`{d}`' for d in defining)} matches what those "
                    f"modules actually define: {listed}. Use the real names and "
                    "signatures — do not add fallbacks or try/except around the "
                    "imports."
                ),
                target_nodes=self._node_store.list_nodes(file_path),
                context_nodes=[
                    node
                    for path in defining
                    for node in self._node_store.list_nodes(path)
                ],
                depends_on=[],
                agent_type=self._default_agent_type or "",
            ))
        return tasks

    # -- helpers -----------------------------------------------------------

    def _require_scheduler(self) -> Scheduler:
        if self._scheduler is None:
            raise SessionError("no plan installed; call plan() or install_plan() first")
        return self._scheduler


def _extract_sig(source: str) -> str | None:
    """Return a normalized ``name(args) -> ret`` signature for the first function.

    Returns ``None`` if the source cannot be parsed or contains no function.
    Used to detect whether a committed edit changed a function's public contract.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ast.unparse(node.args)
            ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            return f"{node.name}({args}){ret}"
    return None


def _is_excluded(rel: str, exclude_patterns: tuple[str, ...]) -> bool:
    """Whether a path (relative to the work dir) matches any exclude glob."""
    return any(
        fnmatch.fnmatch(rel, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]))
        for pattern in exclude_patterns
    )


def _symbol_of(qualified_name: str) -> str:
    """Short symbol name of a node id's name segment, without ingestion suffixes.

    ``FruitManager.apple#2`` -> ``apple``. The ``#n`` disambiguation suffix has to
    go: it is not a word character, so a regex built from it can never match.
    """
    return qualified_name.split("#", 1)[0].rsplit(".", 1)[-1]


def _defined_symbol_names(source: str) -> set[str]:
    """Names a module defines that could be node ids: functions, classes, methods.

    Deliberately **not** module-level assignments. Ingestion only ever creates
    ``function`` / ``class`` / ``method`` nodes, so those names are exactly what a
    symbol-level target contributes to the cross-file scan, and a whole-file target
    must contribute the same set or the two disagree about one file depending on
    how it happens to be stored.

    Including assignments made this the single most expensive line in a real run:
    most well-formed modules declare ``__all__``, so a whole-file target on any one
    of them dragged in every other module that declares one — 123.7 KB of 151 KB in
    the worst observed bundle, matched on nothing but that name.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.ClassDef):
            names.add(stmt.name)
            stack.extend(stmt.body)  # methods are node ids too
    return names


# Match-quality bounds for the cross-file layer. Both are about *evidence*, not
# cost — the byte budget is the cost dial (``session.cross_file_context_bytes``).
# A symbol shorter than this is a word, not a name a relationship can be inferred
# from: ``run`` matched six unrelated files in one real run.
_MIN_SYMBOL_LEN = 4
# A symbol that appears in more than this many nodes says nothing about which of
# them is related to the target, so it is discarded entirely rather than dragging
# every match in behind it.
_MAX_SYMBOL_MATCHES = 8

_CONTEXT_KEYS = ("write_source", "read_source", "read_api")


def _layer_report(
    layers: dict[str, list[str]], context: dict[str, str]
) -> dict[str, dict[str, object]]:
    """Summarize each enrichment layer's contribution for the dispatch event.

    Node ids, not just counts: the acceptance for this is that the layer which put
    a node in a bundle is readable from the log *alone*.
    """
    return {
        name: {
            "count": len(keys),
            "bytes": sum(len(context[k]) for k in keys),
            "nodes": [k.split(":", 1)[1] for k in keys],
        }
        for name, keys in layers.items()
    }


def _context_has(context: dict[str, str], node_id: NodeId) -> bool:
    """Whether a bundle's context already carries ``node_id`` under any key."""
    return any(f"{prefix}:{node_id}" in context for prefix in _CONTEXT_KEYS)


def _context_counts(context: dict[str, str]) -> dict[str, int]:
    """Count a bundle's context entries per key prefix, for the dispatch event."""
    counts = dict.fromkeys(_CONTEXT_KEYS, 0)
    for key in context:
        prefix = key.split(":", 1)[0]
        if prefix in counts:
            counts[prefix] += 1
    return counts


def _file_of(node_id: str) -> str:
    """Return the file path component of a ``file::kind::name`` node id."""
    return node_id.split("::", 1)[0]


def _is_header_id(node_id: str) -> bool:
    """Whether a node id refers to a ``module_header`` fragment."""
    parts = node_id.split("::")
    return len(parts) >= 2 and parts[1] == "module_header"
