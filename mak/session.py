"""Session: orchestrate the full MAK pipeline over a node store and lock table.

A ``Session`` drives the end-to-end flow (PLANS.md §11): **init** ingests the
codebase into the node store; **run** plans the work (optionally with HitL review),
then loops the scheduler — dispatching tasks, validating each agent's staged
fragments with the conflict detector, committing on success, reconstructing the
affected files, and recording an audit commit; **teardown** runs the test suite and
pushes if green.

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
from collections.abc import Callable
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
    NodeId,
    SubTask,
    TaskBundle,
    TaskResult,
)
from mak.git_integration.git import GitHelper
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

    def expire_stale(self) -> list[LockEntry]:
        """Expire and return timed-out leases."""
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

    ``blocked`` lists tasks that were neither completed nor explicitly failed —
    they were stranded by an unsatisfiable DAG or locks that never freed. A run
    with any blocked tasks ends in ``FAILED``, never ``COMPLETED`` (RA-7).
    """

    state: SessionState
    completed: tuple[str, ...]
    failed: tuple[str, ...]
    blocked: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True only when the run completed with no failed or blocked tasks."""
        return (
            self.state is SessionState.COMPLETED
            and not self.failed
            and not self.blocked
        )


class _RecordingRunner:
    """Wraps an agent runner: enriches each bundle, then captures its result.

    The Wave-2 scheduler builds a skeletal ``TaskBundle`` (target node ids only).
    This wrapper — which the session owns and which has the node store — enriches
    the bundle with source context before the agent sees it, and records the
    (enriched bundle, result) pair for the session's collection phase.
    """

    def __init__(
        self,
        inner: _Assigner,
        sink: list[tuple[TaskBundle, TaskResult]],
        enrich: Callable[[TaskBundle], TaskBundle],
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._enrich = enrich

    def assign(self, adapter: object, task: object) -> object:
        bundle = self._enrich(cast(TaskBundle, task))
        result = cast(TaskResult, self._inner.assign(adapter, bundle))
        self._sink.append((bundle, result))
        return result


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
        planner: Planner | None = None,
        git_helper: GitHelper | None = None,
        logger: SessionLogger | None = None,
        test_runner: TestRunner | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.session_id = session_id
        self._config = config
        self._node_store = node_store
        self._lock_table = lock_table
        self._registry = registry
        self._agent_runner = agent_runner
        self._conflict_detector = conflict_detector or ConflictDetector()
        self._planner = planner
        self._git = git_helper
        self._logger = logger
        self._test_runner = test_runner
        self._max_attempts = max_attempts

        self.state = SessionState.CREATED
        self._scheduler: Scheduler | None = None
        self._progress: dict[str, SubTaskProgress] = {}
        self._pending_results: list[tuple[TaskBundle, TaskResult]] = []
        self._partial_queue: list[str] = []
        self._completed: list[str] = []
        self._failed: list[str] = []

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

    # -- phase 1: initialize ----------------------------------------------

    def initialize(self) -> list[NodeId]:
        """Ingest the working directory's Python files into the node store."""
        if self.state is not SessionState.CREATED:
            raise SessionError(f"cannot initialize from state {self.state}")
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
        """Build the DAG + scheduler from a ready plan (bypasses the planner)."""
        if self.state not in (SessionState.INITIALIZED, SessionState.PLANNED):
            raise SessionError(f"cannot install a plan from state {self.state}")
        dag = DAG(subtasks)
        runner = _RecordingRunner(
            self._agent_runner, self._pending_results, self._enrich_bundle
        )
        self._scheduler = Scheduler(
            dag,
            self._lock_table,
            runner,
            self._registry,
            persist_path=self._mak_dir / "task_graph.json",
        )
        self._progress = {
            t.task_id: SubTaskProgress(t.task_id, list(t.target_nodes))
            for t in subtasks
        }
        self.state = SessionState.PLANNED

    # -- phase 3: run ------------------------------------------------------

    def run(self, max_iterations: int = 1000) -> SessionResult:
        """Drive the scheduler loop until the DAG is done or progress stalls."""
        if self.state is not SessionState.PLANNED:
            raise SessionError(f"cannot run from state {self.state}")
        scheduler = self._require_scheduler()
        self.state = SessionState.RUNNING

        for _iteration in range(max_iterations):
            progressed = bool(scheduler.tick())
            while self._pending_results:
                bundle, result = self._pending_results.pop(0)
                self._process_result(bundle, result)
                progressed = True
            if self._partial_queue:
                self._dispatch_partials()
                progressed = True
            if scheduler.is_done() and not self._partial_queue:
                break
            if not progressed:
                break  # stalled: blocked on locks or an unrecoverable failure

        # A task that is neither completed nor explicitly failed was stranded
        # (unsatisfiable deps or locks that never freed). It must NOT be reported
        # as success — the run is COMPLETED only when the DAG is genuinely done.
        accounted = set(self._completed) | set(self._failed)
        blocked = [
            tid for tid in scheduler.dag.remaining() if tid not in accounted
        ]
        if scheduler.is_done() and not self._failed and not blocked:
            self.state = SessionState.COMPLETED
        else:
            self.state = SessionState.FAILED
        if blocked:
            self._log(EventType.SESSION_ENDED, blocked=blocked, stalled=True)
        return SessionResult(
            state=self.state,
            completed=tuple(self._completed),
            failed=tuple(self._failed),
            blocked=tuple(blocked),
        )

    def _process_result(self, bundle: TaskBundle, result: TaskResult) -> None:
        """Validate, commit, and account one agent result (complete/partial/fail)."""
        task_id = bundle.task_id
        progress = self._progress[task_id]
        progress.attempts += 1
        in_scope = set(progress.target_nodes)
        staged = [n for n in result.modified_nodes if n in in_scope]

        committed = self._validate_and_commit(task_id, staged) if result.success else []
        for node_id in committed:
            progress.completed_nodes.add(node_id)
            self._release_lock(task_id, node_id)

        if progress.is_complete:
            self._finish_task(task_id)
        else:
            self._handle_incomplete(progress)

    def _validate_and_commit(self, task_id: str, staged: list[NodeId]) -> list[NodeId]:
        """Validate, then transactionally commit staged fragments (RA-2).

        Order matters: conflict detection → *prospective* reconstruction validated
        against ``ast.parse`` → commit → write files. The store is only advanced
        once the would-be file is known to be valid Python, and a write failure
        after commit reverts the commit so disk and store never diverge.
        """
        if not staged:
            return []
        report = self._conflict_detector.detect(self._build_edit_round(staged))
        if not report.ok:
            self._reject(task_id, staged, report.reasons)
            return []
        if not self._preview_is_valid(staged):
            self._reject(
                task_id, staged, ["reconstruction would produce invalid Python"]
            )
            return []

        committed: list[NodeId] = []
        for node_id in staged:
            self._node_store.commit_node(node_id)
            committed.append(node_id)
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
        """Build a file's prospective source: committed fragments + staged swaps."""
        fragments = []
        seen: set[NodeId] = set()
        for node_id in self._node_store.list_nodes(file_path):
            seen.add(node_id)
            if node_id in staged_set:
                fragments.append(
                    self._node_store.get_staged(node_id)
                    or self._node_store.get_node(node_id)
                )
            else:
                fragments.append(self._node_store.get_node(node_id))
        # Brand-new staged nodes have no committed order slot yet (best-effort).
        for node_id in staged_set - seen:
            staged_fragment = self._node_store.get_staged(node_id)
            if staged_fragment is not None:
                fragments.append(staged_fragment)
        return assemble_fragments(fragments)

    def _build_edit_round(self, staged: list[NodeId]) -> EditRound:
        """Collect staged fragment sources into an EditRound for the detector."""
        sources: dict[str, str] = {}
        for node_id in staged:
            fragment = self._node_store.get_staged(node_id)
            if fragment is not None:
                sources[str(node_id)] = fragment.source
        # Each staged source is both a definition authority and a caller, so the
        # detector validates the task's new calls against its own new signatures.
        return EditRound(definitions=dict(sources), callers=dict(sources))

    def _reconstruct_affected(self, nodes: list[NodeId]) -> list[str]:
        """Rewrite each file touched by ``nodes`` from its committed fragments."""
        files = sorted({str(n).split("::", 1)[0] for n in nodes})
        for file_path in files:
            fragments = self._node_store.get_committed_fragments(file_path)
            if fragments:
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
            self._log(EventType.TASK_COMPLETED, task_id=progress.task_id, failed=True)
        else:
            # Remaining nodes are still locked from the original acquisition; queue
            # a narrowed re-dispatch covering only what is left.
            self._partial_queue.append(progress.task_id)

    def _dispatch_partials(self) -> None:
        """Re-dispatch the narrowed remaining grants of each partial task."""
        queue, self._partial_queue = self._partial_queue, []
        for task_id in queue:
            progress = self._progress[task_id]
            task = self._dag_task(task_id)
            adapter = self._registry.get(task.agent_type)
            bundle = self._enrich_bundle(
                TaskBundle(
                    task_id=task_id,
                    description=task.description,
                    target_nodes=progress.remaining,
                )
            )
            result = cast(TaskResult, self._agent_runner.assign(adapter, bundle))
            self._pending_results.append((bundle, result))

    def _enrich_bundle(self, bundle: TaskBundle) -> TaskBundle:
        """Attach current source for the task's write targets and read context.

        The agent receives the source of every node it will modify (so it edits
        with full sight of the current code) plus the source of the task's
        ``context_nodes`` (sibling methods, attributes, imports) as read-only
        context — addresses the "blind edits" risk (RA-5 / PLANS §8).
        """
        task = self._dag_task(bundle.task_id)
        context = dict(bundle.context)
        for node_id in bundle.target_nodes:
            source = self._node_source(node_id)
            if source is not None:
                context[f"write_source:{node_id}"] = source
        for node_id in task.context_nodes:
            source = self._node_source(node_id)
            if source is not None:
                context[f"read_source:{node_id}"] = source
        return replace(bundle, context=context)

    def _node_source(self, node_id: NodeId) -> str | None:
        """Return a node's current committed source, or None if it does not exist."""
        try:
            return self._node_store.get_node(node_id).source
        except NodeStoreError:
            return None

    def _release_lock(self, task_id: str, node_id: NodeId) -> None:
        self._lock_table.release(node_id, LockMode.WRITE, task_id)

    def _dag_task(self, task_id: str) -> SubTask:
        return self._require_scheduler().dag.get_task(task_id)

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
            runner = _RecordingRunner(
                self._agent_runner, self._pending_results, self._enrich_bundle
            )
            scheduler = Scheduler.from_persisted(
                graph_path,
                self._lock_table,
                runner,
                self._registry,
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

    # -- helpers -----------------------------------------------------------

    def _require_scheduler(self) -> Scheduler:
        if self._scheduler is None:
            raise SessionError("no plan installed; call plan() or install_plan() first")
        return self._scheduler


def _is_excluded(rel: str, exclude_patterns: tuple[str, ...]) -> bool:
    """Whether a path (relative to the work dir) matches any exclude glob."""
    return any(
        fnmatch.fnmatch(rel, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]))
        for pattern in exclude_patterns
    )
