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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from mak.agent_runner.registry import AdapterRegistry
from mak.config import MakConfig
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
from mak.node_store.reconstruction import assemble_fragments, reconstruct_file
from mak.node_store.store import NodeStore
from mak.planner.planner import Planner
from mak.planner.review import display_plan_for_review
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
    failure_reasons: dict[str, str] = field(default_factory=dict)

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
        enrich: Callable[[TaskBundle], TaskBundle],
    ) -> None:
        self._inner = inner
        self._executor = executor
        self._completions = completions
        self._enrich = enrich

    def assign(self, adapter: object, task: object) -> object:
        bundle = self._enrich(cast(TaskBundle, task))
        self._executor.submit(self._run, adapter, bundle)
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
        heartbeat_interval_s: float | None = None,
        collect_timeout_s: float = 300.0,
    ) -> None:
        self.session_id = session_id
        self._config = config
        self._default_agent_type = default_agent_type
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
        # Per-wave commit log: node_id → (source_before, source_after).
        # Populated during run(); read by detect_cascade_tasks() after run().
        self._wave_committed: dict[NodeId, tuple[str | None, str]] = {}

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
        ns_cfg = self._config.node_store
        for pattern in ns_cfg.include_patterns:
            for path in sorted(self._work_dir.glob(pattern)):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(self._work_dir))
                if _is_excluded(rel, ns_cfg.exclude_patterns):
                    continue
                try:
                    self._node_store.parse_file_into_nodes(
                        rel, path.read_text(encoding="utf-8")
                    )
                except SyntaxError:
                    continue
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
        self._log(EventType.SESSION_STARTED, node_count=len(inventory))
        return inventory

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
        subtasks = self._planner.decompose(user_task, self._node_store.list_nodes())
        if review:
            subtasks = display_plan_for_review(
                subtasks, prompt_fn=prompt_fn, printer=printer
            )
        self.install_plan(subtasks)
        return subtasks

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
        self._wave_committed = {}
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
        """Route tasks with no explicit ``agent_type`` to the configured default.

        Without this a planner output that omits ``agent_type`` would reach
        ``registry.get("")`` and crash dispatch with ``UnknownAgentTypeError``.
        """
        if self._default_agent_type is None:
            return subtasks
        return [
            t if t.agent_type else replace(t, agent_type=self._default_agent_type)
            for t in subtasks
        ]

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
        return SessionResult(
            state=self.state,
            completed=tuple(self._completed),
            failed=tuple(self._failed),
            blocked=tuple(blocked),
            skipped=tuple(skipped),
            failure_reasons={
                t: self._failure_reasons[t]
                for t in self._failed
                if t in self._failure_reasons
            },
        )

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
        if result.success:
            self._stage_returned_sources(in_scope, result.new_sources)
        elif result.error:
            # The agent call itself failed (API error, or a truncated/malformed
            # structured response). Keep the reason so the run can report it.
            self._failure_reasons[task_id] = result.error
        # A node is committable only if a pending fragment actually exists for it —
        # either staged here from the agent's returned source, or put directly by a
        # test/local runner. An id the agent *claims* it changed but provided no
        # source for is silently dropped (the task stays incomplete and retries),
        # so a misbehaving agent can never crash the commit phase.
        reported = dict.fromkeys([*result.modified_nodes, *result.new_sources])
        staged = [
            n
            for n in reported
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

        # Accept a no-op: an "audit/review" task may inspect an already-correct file
        # and legitimately return success with no changes. If the agent succeeded,
        # claimed no edits, the target already exists, AND the file it lives in
        # currently parses as valid Python, treat it as done. The validity check
        # is critical: without it, an agent that returns success+no-changes on a
        # task whose file has a syntax error (the exact bug it was sent to fix)
        # would be silently accepted as complete, leaving the error in place.
        if result.success and not result.modified_nodes and not result.new_sources:
            for node_id in progress.target_nodes:
                if (
                    node_id not in progress.completed_nodes
                    and self._target_exists(node_id)
                    and self._file_is_syntactically_valid(node_id)
                ):
                    progress.completed_nodes.add(node_id)
                    self._release_lock(task_id, node_id)

        if progress.is_complete:
            self._finish_task(task_id)
        else:
            self._handle_incomplete(progress)
        return committed_sources

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
        peers) → *prospective* reconstruction validated against ``ast.parse`` →
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
        self._log(EventType.CONFLICT_DETECTED, task_id=task_id, reasons=reasons)
        if reasons:
            self._failure_reasons[task_id] = "; ".join(reasons)
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
                ast.parse(self._assemble_preview(file_path, staged_set))
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

    def _handle_incomplete(self, progress: SubTaskProgress) -> None:
        """Retry remaining grants, or fail the task once attempts are exhausted."""
        scheduler = self._require_scheduler()
        if progress.attempts >= self._max_attempts:
            scheduler.on_task_failed(progress.task_id, requeue=False)
            self._failed.append(progress.task_id)
            reason = self._failure_reasons.setdefault(
                progress.task_id,
                "agent reported success but staged no usable source "
                f"after {progress.attempts} attempt(s)",
            )
            self._log(
                EventType.TASK_COMPLETED,
                task_id=progress.task_id,
                failed=True,
                reason=reason,
            )
        else:
            # Remaining nodes are still locked from the original acquisition; queue
            # a narrowed re-dispatch covering only what is left.
            self._partial_queue.append(progress.task_id)

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
            )
            runner.assign(adapter, bundle)

    def _enrich_bundle(self, bundle: TaskBundle) -> TaskBundle:
        """Attach write targets, planner context, and all dependency sources.

        Four layers of context, each only adding entries not already present:

        1. ``write_source:<id>`` — every node the agent will modify.
        2. ``read_source:<id>`` — nodes the planner explicitly listed as context.
        3. ``read_source:<id>`` — all other nodes in the same file as any write
           target.  Gives the agent full sight of imports, siblings, and class
           structure without relying on the planner.
        4. ``read_source:<id>`` — nodes in *other* files whose source contains
           any target symbol name (word-boundary match).  Captures cross-file
           callers and callees so the agent is never blind to dependencies that
           live outside its own file.
        """
        task = self._dag_task(bundle.task_id)
        context = dict(bundle.context)

        # Layer 1: write targets
        for node_id in bundle.target_nodes:
            source = self._node_source(node_id)
            if source is not None:
                context[f"write_source:{node_id}"] = source

        # Layer 2: planner-specified context
        for node_id in task.context_nodes:
            source = self._node_source(node_id)
            if source is not None:
                context[f"read_source:{node_id}"] = source

        # Layer 3: same-file siblings
        target_files: set[str] = set()
        for node_id in bundle.target_nodes:
            file_path = str(node_id).split("::", 1)[0]
            target_files.add(file_path)
            for sibling_id in self._node_store.list_nodes(file_path):
                write_key = f"write_source:{sibling_id}"
                read_key = f"read_source:{sibling_id}"
                if write_key not in context and read_key not in context:
                    source = self._node_source(sibling_id)
                    if source is not None:
                        context[read_key] = source

        # Layer 4: cross-file references — nodes in other files that mention
        # any target symbol by name, covering callers and callees across the
        # whole codebase.  Symbol = rightmost segment of the qualified name
        # (e.g. "apple" from "FruitManager.apple" or "apple").
        symbols: set[str] = set()
        for nid in bundle.target_nodes:
            parts = str(nid).split("::")
            if len(parts) >= 3:
                symbols.add(parts[2].rsplit(".", 1)[-1])
        if symbols:
            symbol_re = re.compile(
                r"\b(?:" + "|".join(re.escape(s) for s in symbols) + r")\b"
            )
            for xfile_id in self._node_store.list_nodes():
                if str(xfile_id).split("::", 1)[0] in target_files:
                    continue  # same-file already handled in layer 3
                write_key = f"write_source:{xfile_id}"
                read_key = f"read_source:{xfile_id}"
                if write_key not in context and read_key not in context:
                    source = self._node_source(xfile_id)
                    if source and symbol_re.search(source):
                        context[read_key] = source

        return replace(bundle, context=context)

    def _stage_returned_sources(
        self, in_scope: set[NodeId], new_sources: dict[NodeId, str]
    ) -> None:
        """Stage each rewritten source the agent returned over the wire.

        This is the agent→store transport: an API/CLI agent reports the full new
        source of each node it changed, and the session ``put_node``s it (as a new
        pending version) so the normal validate→commit path applies it. Sources for
        nodes outside the task's grant are ignored — an agent may not edit beyond
        the nodes it was authorized to modify.
        """
        for node_id, source in new_sources.items():
            if node_id not in in_scope:
                continue
            self._node_store.put_node(
                node_id, NodeFragment(node_id, self._node_kind(node_id), source, 1)
            )

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
            ast.parse(self._assemble_preview(file_path, set()))
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

        Returns an empty list when no signatures changed, which is the expected
        outcome when the planner was thorough about including all affected nodes.
        A non-empty return is a signal that the planner missed callers; the
        caller (``__main__``) should present these tasks to the user for review
        before running a second wave.
        """
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
            return []

        tasks: list[SubTask] = []
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


def _file_of(node_id: str) -> str:
    """Return the file path component of a ``file::kind::name`` node id."""
    return node_id.split("::", 1)[0]


def _is_header_id(node_id: str) -> bool:
    """Whether a node id refers to a ``module_header`` fragment."""
    parts = node_id.split("::")
    return len(parts) >= 2 and parts[1] == "module_header"
