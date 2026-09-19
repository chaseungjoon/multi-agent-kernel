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
import difflib
import fnmatch
import hashlib
import queue
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
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
from mak.conflict_detector.attribute_check import check_module_attributes
from mak.conflict_detector.constructor_check import check_constructors
from mak.conflict_detector.cross_module_check import (
    CrossModuleDefect,
    check_cross_module_api,
)
from mak.conflict_detector.cycle_check import check_new_cycles
from mak.conflict_detector.detector import ConflictDetector, EditRound
from mak.conflict_detector.duplicate_check import CreatedFunction, check_duplicates
from mak.conflict_detector.module_index import ModuleIndex
from mak.conflict_detector.override_check import check_overrides
from mak.core.atomic import write_text_atomic
from mak.core.exceptions import (
    ContractError,
    GitIntegrationError,
    NodeStoreError,
    PlannerFailedError,
    SchedulingError,
    SemanticGateError,
    SessionError,
    UnsafeNodeIdError,
    WorkTreeConflictError,
)
from mak.core.logging import EventType, SessionLogger
from mak.core.paths import (
    DEFAULT_MAK_DIR_NAME as _MAK_DIR_NAME,
)
from mak.core.paths import (
    check_node_id,
    safe_path_under,
    unsafe_node_id_reason,
)
from mak.core.types import (
    LockEntry,
    LockMode,
    NodeFragment,
    NodeId,
    SubTask,
    TaskBundle,
    TaskResult,
)
from mak.execution_result import ExecutionResult
from mak.git_integration.git import GitHelper
from mak.lock_manager.deadlock_detector import DeadlockDetector
from mak.lock_manager.project_lease import ProjectLease
from mak.lock_manager.resources import api_resource
from mak.node_store.api_digest import api_fingerprint, public_api_digest
from mak.node_store.ingestion import iter_source_files
from mak.node_store.makignore import MakIgnore, ensure_makignore, load_makignore
from mak.node_store.reconstruction import assemble_fragments, reconstruct_file
from mak.node_store.store import FileSyncReport, NodeStore, source_digest
from mak.node_store.transaction import (
    finish,
    install_files,
    mark_installed,
    render_affected,
)
from mak.node_store.transaction import recover as recover_commit
from mak.planner.contracts import contract_stub, implementation_mismatch
from mak.planner.depgraph import DepGraph, dep_graph_from_store
from mak.planner.llm import build_planner_llm
from mak.planner.planner import Planner, PlannerLLM
from mak.planner.review import display_plan_for_review
from mak.planner.validation import PlanFinding, PlanSemantics, validate_plan
from mak.scheduler.dag import DAG
from mak.scheduler.lock_policy import LEGACY_POLICY, LockPolicy, lock_requests
from mak.scheduler.scheduler import Scheduler
from mak.semantic.adjudicator import Adjudicator
from mak.semantic.cascade_graph import CascadeItem, cascade_items
from mak.semantic.contracts import (
    CONTRACT_PREFIX,
    render_contract,
    soft_edges,
    visible_contracts,
)
from mak.semantic.gate_types import GateFinding, ProcessRunner, WaveView, run_process
from mak.semantic.gates import GateSuite
from mak.semantic.interface import changed_bindings
from mak.semantic.locking import build_lock_policy, registrar_kinds
from mak.semantic.read_set import (
    ReadMark,
    ReadSet,
    build_read_set,
    read_set_from_json,
    read_set_to_json,
)
from mak.semantic.registry_merge import MergeKind, plan_merge
from mak.semantic.sources import StoreSources
from mak.semantic.stale import (
    NodeDecision,
    StaleDecision,
    StaleRead,
    Verdict,
    classify,
    decide,
    retry_note,
)
from mak.semantic.symbols import (
    SymbolChangeKind,
    diff_symbols,
    symbol_source,
    symbol_table,
)
from mak.teardown import SuiteOutcome, TeardownResult, may_push

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
    # Wave 20. Set when the kernel — not the agent — sent the attempt back: a
    # stale read (the diff of what changed) or a broken interface promise. It
    # outranks every agent-side reason, because it is the one thing the next
    # attempt must act on, and ``error_kind`` says which it was.
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
    # rate). Empty for a session that never ran. See ``Session._finalize``.
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
class _Completion:
    """One finished agent call: the bundle that was dispatched and its result."""

    bundle: TaskBundle
    result: TaskResult


@dataclass(frozen=True, slots=True)
class _Parked:
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
        project_lease: ProjectLease | None = None,
        gate_runner: ProcessRunner | None = None,
        adjudicator_llm: PlannerLLM | None = None,
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
        # Exclusive ownership of this project's state, taken before anything
        # reads or mutates it. ``None`` leaves a session unguarded, which is
        # what the tests that build one directly want; every real front end
        # supplies one.
        self._project_lease = project_lease
        # The most recent wave's result, kept so teardown can gate a push on
        # something even when the caller has no aggregate to hand it.
        self._last_result: SessionResult | None = None

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
        # Per-wave commit log: node_id → (source_before, source_after). A node a
        # commit *removed* — a fragment superseded by a whole-file write — is
        # recorded with ``None`` after, so post-wave analysis sees deletions.
        # Populated during run(); read by detect_cascade_tasks() after run().
        self._wave_committed: dict[NodeId, tuple[str | None, str | None]] = {}
        # Wave 20. Each touched file as it was before its first commit this
        # wave (``None``: it did not exist), which task(s) committed to it, and
        # which task last committed each node — the "before" side of every
        # post-wave symbol diff and baseline, and the pair a fix-up names.
        self._wave_file_before: dict[str, str | None] = {}
        self._wave_file_writers: dict[str, list[str]] = {}
        self._wave_node_writer: dict[NodeId, str] = {}
        # Post-wave findings cached by store generation: the cascade loop asks
        # twice for the same state, and the checks are not free.
        self._defects_at: tuple[int, list[CrossModuleDefect]] | None = None
        self._cascade_at: tuple[int, list[CascadeItem]] | None = None
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
        # Set when the run loop gives up on an unresponsive worker, so teardown
        # cancels instead of joining it (see ``close``).
        self._wedged = False
        # Agent tokens spent, summed from what each provider reported on its own
        # response. Read off ``TaskResult.usage`` rather than by patching the SDK:
        # the patch-based counter hooked ``Messages.create`` while both the agent
        # adapter and the planner call ``messages.stream``, so it reported zero
        # for the default provider — and patching a vendor's internals is one
        # refactor away from doing that again.
        self._agent_usage: Counter[str] = Counter()
        # ``symbol -> node ids`` for the cross-file enrichment layer, and the
        # store generation it was built from. See ``_symbol_index``.
        self._symbol_index_cache: dict[str, list[NodeId]] = {}
        self._symbol_index_at: int = -1
        # Files that already had committed nodes when this wave's plan was
        # installed. A no-op assertion about anything else is an assertion about
        # code that did not exist to be inspected — see ``_noop_refusal``.
        self._preexisting_files: set[str] = set()
        # Set when the token ceiling stopped the run, so ``_finalize`` can name
        # the budget instead of reporting an unexplained set of stranded tasks.
        self._budget_stop: str | None = None
        # The project's ``.makignore``, read at ``initialize``. Empty until then,
        # so a session driven without initialize ignores nothing extra.
        self._makignore = MakIgnore()
        # Wave 20. The reference graph the current wave was planned against
        # (interface locks and post-wave cascade both read it), the lock policy
        # the wave runs under, and what each dispatched task was granted —
        # commit re-validation must check the *modes* it was given, and a keyed
        # registrar target is held INTENT_WRITE, not WRITE.
        self._wave_graph: DepGraph | None = None
        self._lock_policy: LockPolicy = LEGACY_POLICY
        self._granted: dict[str, dict[NodeId, LockMode]] = {}
        # task id -> every node its latest bundle carried, with the version and
        # digest it was shipped at. Validated at commit (``_reads_are_current``).
        self._read_sets: dict[str, ReadSet] = {}
        # Stale-read accounting for the wave, reported by ``_plan_metrics``.
        self._stale_reads = 0
        self._stale_redispatches = 0
        # Optional LLM adjudicator for uncertain stale reads (``semantic.
        # adjudicator``); built lazily by ``_install_adjudicator``.
        self._adjudicate_fn: (
            Callable[[StaleRead, dict[NodeId, str]], bool | None] | None
        ) = None
        # Results that finished but cannot commit until another in-flight task
        # releases a lock (see ``_park``), and the reason each is waiting.
        self._parked: dict[str, _Parked] = {}
        self._deferring: dict[str, str] = {}
        self._waiting_on_providers: set[str] = set()
        # Optional heavy gates (D3/D4/D6) and the adjudicator (D7). The runner
        # and the model are injectable so tests need neither tools nor keys.
        self._gates = GateSuite(config.semantic, runner=gate_runner or run_process)
        self._adjudicator_llm = adjudicator_llm
        self._adjudicator_instance: Adjudicator | None = None
        self._gates_at: tuple[int, list[GateFinding]] | None = None
        # Per-commit records for rebuilding "pre-wave + these tasks' commits":
        # each touched file's fragments before the wave, and every committed
        # node as (task, node, order, indented source), in commit order.
        self._wave_fragments_before: dict[
            str, list[tuple[NodeId, int | None, str]]
        ] = {}
        self._wave_commit_log: list[tuple[str, NodeId, int | None, str]] = []

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

    @property
    def _journal_dir(self) -> Path:
        """Where a commit-in-flight records what it is about to overwrite.

        One directory per project, not per transaction: only one commit is ever
        in flight at a time (batch commits are applied serially under the store
        lock), and a fixed location is what lets the *next process* find the
        journal a killed one left behind.
        """
        return self._mak_dir / "journal"

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

    def close(self, *, wait: bool = True) -> None:
        """Shut down the worker pool and any agent subprocesses. Repeatable.

        ``wait=False`` is for the abnormal exit. The collect timeout exists so a
        wedged agent cannot stall a run forever — but the shutdown that followed
        it blocked on that very call, so the run hung anyway and the timeout
        bought nothing. Declining to join lets the session report what it has.

        Note what ``cancel_futures`` does and does not do: it drops work still
        *queued*, and cannot interrupt a call already in flight. What bounds that
        one is the per-request SDK timeout threaded through from
        ``AgentConfig.timeout`` — the two are a pair, and neither alone is
        enough. Without the timeout the worker thread would still outlive the
        run, and a pool worker is non-daemon, so the process would refuse to exit
        until the provider gave up on its own.
        """
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
            self._executor = None
            self._concurrent_runner = None
        # Pooled CLI agent subprocesses outlive the thread pool: AgentRunner owns
        # them and its shutdown() had no caller at all, so every agent process a
        # run spawned survived until the interpreter exited — for the whole
        # session, in the long-lived TUI. Duck-typed because the injected
        # _Assigner protocol does not (and need not) declare it.
        shutdown = getattr(self._agent_runner, "shutdown", None)
        if callable(shutdown):
            shutdown()
        # Ownership goes back last, so nothing else can claim the project while
        # this session is still tearing its workers down.
        if self._project_lease is not None:
            self._project_lease.release()

    # -- phase 1: initialize ----------------------------------------------

    def initialize(self) -> list[NodeId]:
        """Take ownership, recover any interrupted commit, and reconcile the tree."""
        if self.state is not SessionState.CREATED:
            raise SessionError(f"cannot initialize from state {self.state}")
        # Ownership first, before anything reads or mutates project state. This is
        # what makes the ``clear()`` below sound: without it, a second startup
        # stripped a *live* session's leases, having established nothing about
        # whether the prior owner was dead.
        self._acquire_project()
        # A crashed run can leave a commit half-installed. Resolve it before the
        # store is read for anything else, so ingestion never reconciles against
        # a working tree that is mid-transaction.
        self._recover_journal()
        # A fresh session owns none of the leases a prior (possibly killed) run left
        # in the persisted lock table; drop them so they don't surface later as
        # spurious "lease expired" warnings. Crash recovery uses recover() instead.
        self._lock_table.clear()
        # Read before pruning and reconciling, so both honor it. When the file does
        # not exist yet its defaults apply in memory; it is only written after the
        # clean-tree check below, which a fresh untracked file would otherwise fail.
        self._makignore = load_makignore(self._work_dir)
        pruned = self.prune_excluded_nodes()
        self._reconcile_work_dir()
        if self._git is not None and self._config.git.require_clean_tree:
            self._require_clean_tree()
        ensure_makignore(self._work_dir)
        self._take_gate_baseline()
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

    def _take_gate_baseline(self) -> None:
        """Record the type checker's pre-existing diagnostics, when it is on."""
        try:
            self._gates.take_baseline(self._work_dir)
        except SemanticGateError as exc:
            self._log(EventType.GATE_FINDING, gate="type_check", error=str(exc))

    def _install_adjudicator(self) -> None:
        """Build (once) and re-budget the stale-read adjudicator for a wave."""
        if self._adjudicator_instance is None:
            llm = self._adjudicator_llm or self._configured_adjudicator_llm()
            if llm is None:
                self._adjudicate_fn = None
                return
            self._adjudicator_instance = Adjudicator(
                llm,
                max_calls=self._config.semantic.adjudicator_max_calls,
                log=lambda **p: self._log(EventType.ADJUDICATION, **p),
            )
        self._adjudicator_instance.reset()
        self._adjudicate_fn = self._adjudicator_instance

    def _configured_adjudicator_llm(self) -> PlannerLLM | None:
        """Build the configured adjudicator model, or None when it cannot be."""
        spec = self._config.semantic.adjudicator
        if spec is None:
            return None
        backend, _, model = spec.partition(":")
        try:
            return build_planner_llm(model, backend=backend)
        except PlannerFailedError as exc:
            self._log(EventType.GATE_FINDING, gate="adjudicator", error=str(exc))
            return None

    def _acquire_project(self) -> None:
        """Take the project's exclusive lease, if one was supplied."""
        if self._project_lease is not None:
            self._project_lease.acquire()

    def _recover_journal(self) -> None:
        """Resolve a commit journal an interrupted run left behind."""
        outcome = recover_commit(
            self._journal_dir,
            self._node_store,
            resolve=self._safe_output_path,
            reaudit=self._reaudit,
        )
        if outcome is not None:
            self._log(EventType.SESSION_STARTED, recovered_commit=outcome)
            print(
                f"mak: recovered an interrupted commit ({outcome}).",
                file=sys.stderr,
            )

    def _reaudit(self, task_id: str, files: list[str]) -> None:
        """Re-run an audit commit whose original run was killed mid-flight."""
        if self._git is None or not self._config.git.auto_commit:
            return
        self._git.commit_task(
            task_id=task_id,
            files=files,
            description="recovered interrupted commit",
            agent_type="recovery",
            session_id=self.session_id,
        )

    def _require_clean_tree(self) -> None:
        """Refuse to start on a dirty tree, when the project asks for that.

        Off by default. A clean-tree precondition is a legitimate product policy
        — it makes ``git diff`` after a run mean exactly "what MAK did" — but it
        is the project's call to make, not one MAK imposes, so it is opt-in via
        ``git.require_clean_tree`` and this is the only thing that enforces it.
        """
        if self._git is None:
            return
        if not self._git.validate_clean_state():
            raise GitIntegrationError(
                "the working tree has uncommitted changes and "
                "git.require_clean_tree is on; commit or stash them first"
            )

    def _reconcile_work_dir(self) -> None:
        """Make the store agree with the working tree before anything runs.

        Ingestion used to be one-directional and blind: it fed each file's
        current source to a store method that *skipped* any file it already held
        as a whole-file node, never removed a symbol that had disappeared, and
        restamped every fragment at version 1. So a human's edit between two
        sessions could be silently discarded, and a function they deleted kept
        being reconstructed. Reconciliation is the fix, in both directions:

        * every included file on disk is synchronized into the store, and
        * every file the store still holds live nodes for that is **gone** from
          disk has those nodes retired.

        A file whose content differs from what MAK last materialized was edited
        by someone else. Under the default ``on_external_edit="adopt"`` the disk
        wins — it is the newer truth, and the store's job is to record it, not
        overrule it. Under ``"conflict"`` the divergence raises *here*, before
        planning, so no agent can be handed content the tree no longer holds.

        The file list comes from a walk that refuses to *descend* into an
        excluded directory. The previous ``glob(pattern)`` produced the same set
        — the exclusion was always applied — but only after enumerating every
        path under ``.venv``, ``node_modules``, ``site-packages`` and
        ``__pycache__``, which on a repo with a populated virtualenv is the bulk
        of ``initialize()``'s cost and every one of those paths is discarded.
        """
        ns_cfg = self._config.node_store
        seen: set[str] = set()
        adopted: list[str] = []
        reports: list[FileSyncReport] = []
        for path in iter_source_files(
            self._work_dir,
            ns_cfg.include_patterns,
            ns_cfg.exclude_patterns,
            skip=self._is_store_path,
            ignore=self._makignore.matches,
        ):
            rel = str(path.relative_to(self._work_dir))
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            seen.add(rel)
            if self._is_external_edit(rel, source):
                self._on_external_edit(rel, source)
                adopted.append(rel)
            try:
                reports.append(self._node_store.sync_file(rel, source))
            except (SyntaxError, OSError):
                continue
        reports.extend(self._retire_missing_files(seen))
        self._report_reconciliation(reports, adopted)

    def _is_external_edit(self, file_path: str, source: str) -> bool:
        """Whether ``source`` differs from the content MAK last wrote there.

        A file MAK has never materialized is not an external edit — it is simply
        a file, and every file is one on the first run.
        """
        recorded = self._node_store.materialized_digest(file_path)
        if recorded is None:
            return False
        return recorded != NodeStore.content_digest(source)

    def _on_external_edit(self, file_path: str, source: str) -> None:
        """Apply the configured policy to a file someone edited outside MAK."""
        if self._config.session.on_external_edit == "conflict":
            raise WorkTreeConflictError(
                f"'{file_path}' has changed since MAK last wrote it "
                f"(recorded {self._node_store.materialized_digest(file_path)}, "
                f"found {NodeStore.content_digest(source)}); "
                "session.on_external_edit is 'conflict'. Review the file, then "
                "re-run with 'adopt' to take the working tree as authoritative."
            )
        self._log(
            EventType.SESSION_STARTED, adopted_external_edit=file_path
        )

    def _retire_missing_files(self, seen: set[str]) -> list[FileSyncReport]:
        """Retire the nodes of every file the store holds that disk no longer has."""
        known = {
            str(node_id).split("::", 1)[0]
            for node_id in self._node_store.list_all_nodes()
        }
        reports: list[FileSyncReport] = []
        for file_path in sorted(known - seen):
            if (self._work_dir / file_path).exists():
                # Present but not walked: excluded, unreadable, or not a source
                # file under the current patterns. Not a deletion — exclusion
                # pruning is a separate, deliberate operation.
                continue
            reports.append(self._node_store.sync_file(file_path, None))
        return reports

    def _report_reconciliation(
        self, reports: list[FileSyncReport], adopted: list[str]
    ) -> None:
        """Say what reconciliation changed, when it changed anything."""
        updated = sum(len(r.updated) for r in reports)
        retired = sum(len(r.retired) for r in reports)
        if not adopted and not retired and not updated:
            return
        self._log(
            EventType.SESSION_STARTED,
            reconciled_files=len(adopted),
            reconciled_updated=updated,
            reconciled_retired=retired,
        )
        if adopted or retired:
            print(
                f"mak: reconciled the node store with the working tree — "
                f"{len(adopted)} file(s) edited outside MAK, "
                f"{retired} node(s) retired.",
                file=sys.stderr,
            )

    def _mak_roots(self) -> tuple[Path, ...]:
        """Return the absolute location of MAK's own persistence directory.

        One root, not two. ``mak_dir`` used to be ambiguous — relative to the
        process CWD by one reading and to the work dir by another — so this had
        to treat *both* as MAK's own and hope. ``config.anchor_mak_dir`` now
        settles it before a session is built (a relative ``mak_dir`` means
        "inside the work dir"), which leaves exactly one directory to exclude.
        """
        mak_dir = self._mak_dir
        candidate = mak_dir if mak_dir.is_absolute() else self._work_dir / mak_dir
        try:
            return (candidate.resolve(),)
        except OSError:
            return ()

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
        alternative; this is the one that preserves real work. A path the user
        adds to ``.makignore`` is dropped here the same way.
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
                "(MAK's own .mak/ store, an excluded path, or one in .makignore).",
                file=sys.stderr,
            )
        return len(doomed)

    def _is_excluded_node(self, node_id: str, patterns: tuple[str, ...]) -> bool:
        """Whether a node's file component is excluded from ingestion."""
        file_path = node_id.split("::", 1)[0]
        return (
            self._is_store_path(self._work_dir / file_path)
            or _is_excluded(file_path, patterns)
            or self._makignore.is_ignored(file_path)
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
        self, subtasks: list[SubTask], graph: DepGraph | None = None
    ) -> tuple[list[SubTask], list[PlanFinding]]:
        """Run deterministic plan validation, unless disabled in config."""
        if not self._config.planner.validate:
            return subtasks, []
        if graph is None:
            graph = dep_graph_from_store(self._node_store)
        targets = {node for task in subtasks for node in task.target_nodes}
        semantic = PlanSemantics(
            api_locks=self._config.semantic.api_locks,
            registrar_kinds={
                node: str(kind)
                for node, kind in registrar_kinds(self._node_store, targets).items()
            },
        )
        result = validate_plan(
            subtasks, graph, self._node_store.list_nodes(), semantic=semantic
        )
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
        self._wave_file_before = {}
        self._wave_file_writers = {}
        self._wave_node_writer = {}
        self._wave_fragments_before = {}
        self._wave_commit_log = []
        self._defects_at = None
        self._cascade_at = None
        self._gates_at = None
        self._granted = {}
        self._read_sets = {}
        self._parked = {}
        self._deferring = {}
        self._waiting_on_providers = set()
        self._stale_reads = 0
        self._stale_redispatches = 0
        self._install_adjudicator()
        self._conflict_rejections = 0
        self._redispatches = 0
        self._concurrency_samples = []
        self._dispatches = 0
        self._context_bytes = 0
        self._starved_dispatches = 0
        self._budget_stop = None
        # The wave's starting inventory, captured before anything runs: a target
        # whose file is not in here cannot be the subject of a credible "nothing
        # needed changing", because there was nothing there to look at.
        self._preexisting_files = {
            str(node_id).split("::", 1)[0]
            for node_id in self._node_store.list_nodes()
        }
        # Validate/augment against the code graph (grounds ids, adds missing edges)
        # here — so the CLI's plan() path, the TUI's direct install, cascade waves,
        # and user-edited plans all get validation. Idempotent when plan() already
        # validated the same list.
        self._reject_unsafe_targets(subtasks)
        # Always built: interface locks read callees off it, validation reads
        # references, and post-wave cascade needs the *pre*-wave edges to find
        # callers of symbols the wave deletes.
        self._wave_graph = dep_graph_from_store(self._node_store)
        subtasks, findings = self._validate_subtasks(subtasks, self._wave_graph)
        self.last_plan_findings = findings
        if findings:
            self._log_plan_findings(findings)
        subtasks = self._apply_default_agent(subtasks)
        self._lock_policy = build_lock_policy(
            self._config.semantic, self._node_store, self._wave_graph, subtasks
        )
        soft: dict[str, set[str]] = {}
        if self._config.semantic.contract_dispatch:
            subtasks, soft = soft_edges(subtasks, self._lock_policy)
        dag = DAG(subtasks, soft_edges=soft)
        self._scheduler = Scheduler(
            dag,
            self._lock_table,
            self._runner(),
            self._registry,
            persist_path=self._mak_dir / "task_graph.json",
            max_concurrent=self._max_concurrent,
            lock_policy=self._lock_policy,
        )
        self._progress = {
            t.task_id: SubTaskProgress(t.task_id, list(t.target_nodes))
            for t in subtasks
        }
        self.state = SessionState.PLANNED

    def _reject_unsafe_targets(self, subtasks: list[SubTask]) -> None:
        """Refuse a plan whose targets would write outside the working directory.

        ``parse_plan`` applies the same rule to planner output, but this is the
        funnel every plan passes through — the interactive app installs a plan
        directly, and each cascade wave builds one from scratch. Neither touches
        the planner's parser, so without this check two of the three ways a plan
        reaches the scheduler are ungated.

        Raised rather than corrected: an escaping target is not a typo validation
        can ground, and silently rewriting one would hide what was asked for.
        """
        mak_name = self._mak_dir.name or _MAK_DIR_NAME
        offenders = [
            (task.task_id, str(node), reason)
            for task in subtasks
            for node in task.target_nodes
            if (reason := unsafe_node_id_reason(str(node), mak_dir_name=mak_name))
            is not None
        ]
        if not offenders:
            return
        listed = "; ".join(f"{tid} -> {node} ({why})" for tid, node, why in offenders)
        raise SessionError(
            f"refusing to install a plan with {len(offenders)} target(s) outside "
            f"the working directory: {listed}"
        )

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
                    EventType.AGENT_REMAPPED,
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
        self._wedged = False
        try:
            self._run_loop(scheduler, max_iterations)
        finally:
            stop.set()
            heartbeat.join(timeout=self._heartbeat_interval + 1.0)
            # A wedged worker must not be waited on — that is the hang the
            # collect timeout was supposed to prevent.
            self.close(wait=not self._wedged)

        return self._finalize(scheduler)

    def _run_loop(self, scheduler: Scheduler, max_iterations: int) -> None:
        """Dispatch concurrently, collect batches, and process them to completion."""
        last_deadlock_scan = time.monotonic()
        for _iteration in range(max_iterations):
            if self._budget_breach() is not None:
                self._stop_on_budget(scheduler)
                break
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
            if self._all_in_flight_parked(scheduler):
                self._release_parked_victim(scheduler)
                continue

            batch = self._collect_batch()
            if not batch:
                # Collection timed out with work in flight: a worker is wedged.
                # Recorded so ``close`` does not then block joining it.
                self._wedged = True
                self._log(
                    EventType.SESSION_ENDED,
                    wedged=True,
                    in_flight=sorted(scheduler.dispatched),
                    collect_timeout_s=self._collect_timeout,
                )
                break
            self._process_batch(batch)

    def _budget_breach(self) -> str | None:
        """Describe the spend ceiling this run has passed, or None if it has not.

        Checked between iterations, never inside result processing: a run that
        has overspent must stop *dispatching*, not abandon a commit half-applied.
        The number compared is :attr:`total_tokens` — agents and planner, from
        what each provider reported — so it is the same figure the TUI counter
        and the final report show, and the three cannot disagree.
        """
        ceiling = self._config.session.max_total_tokens
        if ceiling is None:
            return None
        spent = self.total_tokens
        if spent < ceiling:
            return None
        return (
            f"token budget exhausted: {spent} tokens spent against a "
            f"session.max_total_tokens of {ceiling}"
        )

    def _stop_on_budget(self, scheduler: Scheduler) -> None:
        """Stop dispatching, collect what is already in flight, and record why."""
        self._budget_stop = self._budget_breach()
        self._log(
            EventType.SESSION_ENDED,
            budget_exhausted=True,
            max_total_tokens=self._config.session.max_total_tokens,
            total_tokens=self.total_tokens,
            in_flight=sorted(scheduler.dispatched),
        )
        self._finish_in_flight(scheduler)

    def _finish_in_flight(self, scheduler: Scheduler) -> None:
        """Process the results of already-dispatched tasks, dispatching nothing.

        Bounded by the number in flight when it starts rather than by
        ``scheduler.dispatched`` emptying: a partially-completed task re-queues
        itself for a narrower re-dispatch, and this loop deliberately never makes
        that dispatch, so waiting for the set to drain would wait forever. Those
        re-queued partials are dropped and surface as stranded tasks in the
        result, which is what they are.
        """
        pending = len(scheduler.dispatched - set(self._parked))
        while pending > 0:
            batch = self._collect_batch()
            if not batch:
                self._wedged = True
                break
            pending -= len(batch)
            self._process_batch(batch)
        self._partial_queue.clear()
        # A parked result is waiting on a task that will now never be
        # dispatched again; it is stranded like any other unfinished task.
        self._parked.clear()

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

        if (
            scheduler.is_done()
            and not self._failed
            and not blocked
            and not skipped
            and self._budget_stop is None
        ):
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
        result = SessionResult(
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
            stopped_reason=self._budget_stop,
        )
        # Retained so ``teardown`` has something to gate on when its caller does
        # not supply an aggregate. ``install_plan`` resets the per-wave counters
        # this was built from, so the result object is the only thing that
        # survives the next wave being installed.
        self._last_result = result
        return result

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
            "stale_reads": float(self._stale_reads),
            "stale_redispatches": float(self._stale_redispatches),
        }

    @property
    def token_usage(self) -> dict[str, int]:
        """Tokens spent this session: every agent call plus the planner's.

        Sourced from what each provider reported on its own response, so a
        streamed call counts exactly like a non-streamed one. The planner's share
        is included because a run's cost is not only its agents — decomposition,
        its retries, and the optional critique pass are all billed.
        """
        total = Counter(self._agent_usage)
        planner_usage = getattr(self._planner, "token_usage", None)
        if isinstance(planner_usage, dict):
            total.update(
                {k: v for k, v in planner_usage.items() if isinstance(v, int)}
            )
        return dict(total)

    @property
    def total_tokens(self) -> int:
        """Input + output tokens across agents and planner.

        Sums only the two directional counters, never a provider's own "total"
        field, so a backend that reports both cannot be counted twice.
        """
        usage = self.token_usage
        return sum(
            value
            for key, value in usage.items()
            if key in ("input_tokens", "output_tokens")
        )

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
        # Anything this batch completed may have released a lock a parked
        # result was waiting for.
        self._resume_parked()

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
        in_scope = set(progress.target_nodes)
        staged = [
            n
            for n in dict.fromkeys([*reported, *accepted])
            if n in in_scope and self._node_store.get_staged(n) is not None
        ]
        return self._settle(bundle, result, staged, peers)

    def _settle(
        self,
        bundle: TaskBundle,
        result: TaskResult,
        staged: list[NodeId],
        peers: dict[str, str],
    ) -> dict[str, str]:
        """Commit what can be committed and account the attempt's outcome.

        Split from :meth:`_process_one` so a parked result can be settled again
        later without being counted — or logged — as a second agent attempt.
        """
        task_id = bundle.task_id
        progress = self._progress[task_id]
        self._deferring.pop(task_id, None)
        committed = (
            self._validate_and_commit(task_id, staged, peers)
            if result.success
            else []
        )
        if task_id in self._deferring:
            self._park(bundle, result, staged)
            return {}
        committed_sources: dict[str, str] = {}
        for node_id in committed:
            progress.completed_nodes.add(node_id)
            self._release_lock(task_id, node_id)
            source = self._node_source(node_id)
            if source is not None:
                committed_sources[str(node_id)] = source

        refusals: list[str] = []
        if self._is_asserted_noop(result):
            refusals = self._accept_noop(progress, result)

        # Nothing was stageable and the task is still open: say *why*, now, while
        # the returned ids are still in hand. Left to _handle_incomplete this
        # becomes a catch-all string that names no suspect.
        if result.success and not staged and not progress.is_complete:
            self._record_failure(
                task_id, self._describe_empty_result(progress, result, refusals)
            )

        if progress.is_complete:
            self._finish_task(task_id)
        else:
            self._handle_incomplete(progress, result)
        return committed_sources

    # -- parked commits (Wave 20) --------------------------------------------

    def _defer(self, task_id: str, reason: str, *, providers: bool = False) -> None:
        """Mark the commit in progress as waiting for a lock, not failed.

        The agent's work is fine; another in-flight task holds something the
        commit needs. Re-running the agent would spend a whole call — and an
        attempt of the retry budget — to arrive at the same result, so the
        result is parked and its commit retried when locks are released.
        """
        self._deferring[task_id] = reason
        if providers:
            self._waiting_on_providers.add(task_id)
        else:
            self._waiting_on_providers.discard(task_id)

    def _park(
        self, bundle: TaskBundle, result: TaskResult, staged: list[NodeId]
    ) -> None:
        """Take the attempt's staged sources out of the store and park them."""
        task_id = bundle.task_id
        sources: dict[NodeId, str] = {}
        for node_id in staged:
            fragment = self._node_store.get_staged(node_id)
            if fragment is not None:
                sources[node_id] = fragment.source
            self._node_store.rollback_node(node_id)
        reason = self._deferring.pop(task_id, "waiting for a lock")
        self._parked[task_id] = _Parked(
            bundle, result, sources, reason,
            on_providers=task_id in self._waiting_on_providers,
        )
        self._log(EventType.COMMIT_DEFERRED, task_id=task_id, reason=reason)

    def _resume_parked(self) -> None:
        """Retry every parked commit until a pass makes no progress."""
        progressed = True
        while progressed and self._parked:
            progressed = False
            for task_id in sorted(self._parked):
                if self._parked[task_id].on_providers and self._providers_pending(
                    task_id
                ):
                    continue
                parked = self._parked.pop(task_id)
                for node_id, source in parked.sources.items():
                    self._node_store.put_node(
                        node_id,
                        NodeFragment(node_id, self._node_kind(node_id), source, 1),
                    )
                self._log(EventType.COMMIT_DEFERRED, task_id=task_id, resumed=True)
                self._settle(parked.bundle, parked.result, list(parked.sources), {})
                if task_id not in self._parked:
                    progressed = True

    def _all_in_flight_parked(self, scheduler: Scheduler) -> bool:
        """Whether every in-flight task is a parked result (none can progress)."""
        dispatched = scheduler.dispatched
        return (
            bool(self._parked)
            and not self._partial_queue
            and dispatched <= set(self._parked)
        )

    def _release_parked_victim(self, scheduler: Scheduler) -> None:
        """Break a cycle of parked results by re-queueing one of them.

        Parking is the one place a task waits while holding locks, so it is the
        one place a wait can become a cycle: W waits for R's interface read
        lock while R waits to take a table W appends to. The last task by id
        gives its locks back and is re-queued with fresh context; the rest are
        retried at once. A task waiting on its *contract providers* is instead
        re-gated on them, since re-dispatching it at once would only park it
        again.
        """
        victim = sorted(self._parked)[-1]
        parked = self._parked.pop(victim)
        progress = self._progress[victim]
        if parked.on_providers:
            self._granted.pop(victim, None)
            self._log(
                EventType.COMMIT_DEFERRED, task_id=victim, released=True,
                reason="re-gated on its contract providers",
            )
            scheduler.wait_for_dependencies(victim)
            self._resume_parked()
            return
        progress.kernel_note = (
            f"Your previous result could not be committed ({parked.reason}), and "
            "the tasks holding what it needed were waiting on this one in turn. "
            "It was released so they could finish. Redo the task against the "
            "current code in your refreshed bundle."
        )
        progress.error_kind = "commit_cycle"
        self._granted.pop(victim, None)
        self._redispatches += 1
        self._log(EventType.COMMIT_DEFERRED, task_id=victim, released=True)
        scheduler.on_task_failed(victim, requeue=True)
        self._resume_parked()

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

    def _accept_noop(
        self, progress: SubTaskProgress, result: TaskResult
    ) -> list[str]:
        """Close the grants of a task the agent asserted needed no change.

        Returns the reasons any grant was **refused**, for the caller to record.

        Gated on the target existing and its file parsing — an assertion that
        nothing needs changing is not evidence about a file that is missing, or
        one whose syntax error is the very bug the task was sent to fix — and,
        since Wave 18, on the target having been there to inspect at all. See
        :meth:`_noop_refusal`. Everything outside those two cases keeps the
        acceptance path it has always had: this is a narrowing, not a redesign.
        """
        refusals: list[str] = []
        for node_id in progress.target_nodes:
            if node_id in progress.completed_nodes:
                continue
            refusal = self._noop_refusal(progress, node_id)
            if refusal is not None:
                refusals.append(refusal)
                continue
            if not self._target_exists(
                node_id
            ) or not self._file_is_syntactically_valid(node_id):
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
        return refusals

    def _noop_refusal(
        self, progress: SubTaskProgress, node_id: NodeId
    ) -> str | None:
        """Why this grant may not be closed by assertion, or None if it may.

        ``no_changes_required`` is the one completion an agent awards itself, and
        the guard around it was an *existence* check rather than a *work* check:
        an agent that found a task hard could close it by setting one boolean, as
        long as the target file happened to be there and to parse. Two cases where
        the assertion cannot be true whatever the agent believes, both of them
        read off this wave's own plan rather than off the agent's answer:

        - a **dependency created the target**. MAK's own ``depends_on`` edge says
          the file did not exist until an earlier task in this wave wrote it, so
          "I looked and nothing needed changing" describes an inspection that
          could not have happened when the plan was written.
        - a **greenfield whole-file grant on the first attempt**. Same reasoning
          without the edge: the wave itself is what created the file. Only the
          first attempt is refused — a second attempt has seen the retry note and
          the file's real contents, so its assertion is about something.

        Everything else — a target that predates the wave, a later attempt — is
        accepted exactly as before.
        """
        file_path = str(node_id).split("::", 1)[0]
        if file_path in self._preexisting_files:
            return None
        creator = self._dependency_creating(progress.task_id, file_path)
        if creator is not None:
            return (
                f"'{node_id}' did not exist when this wave was planned — task "
                f"'{creator}', which this task depends on, is what created it. "
                "'no changes required' cannot describe code you inspected before "
                "it existed: read the file as it stands now and make the change "
                "this task asks for."
            )
        if "::" not in str(node_id) and progress.attempts <= 1:
            return (
                f"'{node_id}' did not exist when this wave was planned, so there "
                "was nothing to inspect; a first-attempt 'no changes required' on "
                "a whole file this wave itself creates is not an assessment. "
                "Return the file's complete source."
            )
        return None

    def _dependency_creating(self, task_id: str, file_path: str) -> str | None:
        """Return the depended-on task that targets ``file_path``, if any.

        Direct edges only. A transitive ancestor's output has been visible to
        everything downstream of it for at least one commit, so the "nothing
        existed to inspect" argument does not hold there.
        """
        try:
            task = self._dag_task(task_id)
        except (SessionError, SchedulingError, KeyError):
            return None
        for dep_id in task.depends_on:
            try:
                dep = self._dag_task(dep_id)
            except (SessionError, SchedulingError, KeyError):
                continue
            if any(
                str(target).split("::", 1)[0] == file_path
                for target in dep.target_nodes
            ):
                return dep_id
        return None

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
        self._agent_usage.update(
            {k: v for k, v in result.usage.items() if isinstance(v, int)}
        )
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
            repairs=result.repairs,
            error=result.error,
        )

    def _describe_empty_result(
        self,
        progress: SubTaskProgress,
        result: TaskResult,
        noop_refusals: list[str] | None = None,
    ) -> str:
        """Explain why a *successful* agent result left nothing to commit.

        The single catch-all this replaces ("agent reported success but staged no
        usable source") described a symptom shared by four distinct causes, so a
        failed run could not be diagnosed without re-running it.

        A refused no-op is reported first and verbatim: it is the most specific
        answer there is, and it is phrased as the instruction the retry needs.
        Without it the generic tail would tell an agent that *did* assert a no-op
        that it had not — advice that describes no defect it can act on.
        """
        if noop_refusals:
            return "; ".join(noop_refusals)
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
            reconstruct_file(
                fragments, output_path=self._safe_output_path(file_path)
            )
            return True
        except (SyntaxError, OSError, UnsafeNodeIdError):
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
        if not self._providers_committed(task_id):
            if task_id not in self._deferring:
                for node_id in staged:
                    self._node_store.rollback_node(node_id)
            return []
        if not self._reconcile_registrars(task_id, staged):
            return []
        if not self._reads_are_current(task_id, staged):
            return []
        report = self._conflict_detector.detect(
            self._build_edit_round(staged, peers or {}, task_id=task_id)
        )
        if not report.ok:
            self._reject(task_id, staged, report.reasons)
            return []
        if not self._contracts_hold(task_id, staged):
            return []
        if not self._interfaces_are_granted(task_id, staged):
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
            [(node_id, self._granted_mode(task_id, node_id)) for node_id in staged],
            task_id,
        ):
            self._reject(
                task_id, staged, ["write lock lost before commit (lease expired)"]
            )
            return []

        # Everything from here is one transaction. The store's snapshot covers
        # the node versions, the superseded fragments, and the metadata; the
        # journal covers the output files. The metadata save at the end of the
        # ``with`` block is the commit point for both — before it, nothing
        # durable has changed; after it, the change is recoverable in full.
        wave_entries: dict[NodeId, tuple[str | None, str | None]] = {}
        before_files = self._files_before(staged)
        try:
            with self._node_store.transaction():
                for node_id in staged:
                    old_source = self._node_source(node_id)  # before commit
                    superseded = self._superseded_by(node_id)
                    self._node_store.commit_node(node_id)
                    new_source = self._node_source(node_id)  # after commit
                    if new_source is not None:
                        wave_entries[node_id] = (old_source, new_source)
                    for gone, gone_source in superseded.items():
                        if self._node_source(gone) is None:
                            wave_entries[gone] = (gone_source, None)
                installed = install_files(
                    self._node_store,
                    staged,
                    resolve=self._safe_output_path,
                    journal_dir=self._journal_dir,
                    session_id=self.session_id,
                    task_id=task_id,
                )
        except (SyntaxError, OSError, UnsafeNodeIdError, NodeStoreError) as exc:
            # Store and files are both back where they started; the pending
            # fragments are the only thing left to discard.
            for node_id in staged:
                self._node_store.rollback_node(node_id)
            self._log(
                EventType.CONFLICT_DETECTED,
                task_id=task_id,
                reasons=[f"commit transaction rolled back: {exc}"],
                files=sorted({str(n).split("::", 1)[0] for n in staged}),
            )
            self._record_failure(task_id, f"commit transaction rolled back: {exc}")
            return []

        # Past the commit point. Only now is any of this recorded as work that
        # happened: ``_wave_committed`` used to be written before the file step,
        # so a rolled-back transaction left entries behind and the wave's cascade
        # analysis went on to inspect "reverted" work as if it were real.
        self._wave_committed.update(wave_entries)
        self._record_wave_writes(task_id, staged, before_files)
        for file_path, content in installed.contents.items():
            self._node_store.record_materialized(file_path, content)
        mark_installed(installed)
        self._audit_commit(task_id, staged)
        finish(installed)
        return list(staged)

    def _files_before(self, staged: list[NodeId]) -> dict[str, str | None]:
        """Each staged file's committed source, for files first touched now."""
        before: dict[str, str | None] = {}
        for file_path in sorted({_file_of(str(n)) for n in staged}):
            if file_path in self._wave_file_before:
                continue
            fragments = self._node_store.get_committed_fragments(file_path)
            before[file_path] = assemble_fragments(fragments) if fragments else None
            self._wave_fragments_before[file_path] = [
                (f.node_id, self._node_store.node_order(f.node_id), f.source)
                for f in fragments
            ]
        return before

    def _superseded_by(self, node_id: NodeId) -> dict[NodeId, str]:
        """Return the fragments a whole-file commit of ``node_id`` will remove."""
        if "::" in str(node_id):
            return {}
        return {
            fragment: source
            for fragment in self._file_fragment_ids(node_id)
            if (source := self._node_source(fragment)) is not None
        }

    def _record_wave_writes(
        self, task_id: str, staged: list[NodeId], before: dict[str, str | None]
    ) -> None:
        """Remember what this commit touched, once it is past the commit point."""
        for file_path, source in before.items():
            self._wave_file_before.setdefault(file_path, source)
        for node_id in staged:
            self._wave_node_writer[node_id] = task_id
            writers = self._wave_file_writers.setdefault(_file_of(str(node_id)), [])
            if task_id not in writers:
                writers.append(task_id)
        for file_path in sorted({_file_of(str(n)) for n in staged}):
            for fragment in self._node_store.get_committed_fragments(file_path):
                if fragment.node_id in staged:
                    self._wave_commit_log.append((
                        task_id,
                        fragment.node_id,
                        self._node_store.node_order(fragment.node_id),
                        fragment.source,
                    ))

    # -- declared contracts (Wave 20, P3) -----------------------------------

    def _providers_committed(self, task_id: str) -> bool:
        """Hold a contract-dispatched task's commit until its providers commit.

        A task dispatched ahead of a soft dependency was built against the
        provider's *contract*; its code may call what does not exist yet, so it
        is parked until the provider commits — and fails with it if it fails.
        """
        dag = self._require_scheduler().dag
        waiting = sorted(
            dep for dep in dag.soft_dependencies(task_id) if not dag.is_complete(dep)
        )
        failed = [dep for dep in waiting if dep in self._failed]
        if failed:
            self._record_failure(
                task_id,
                f"its contract provider(s) {', '.join(failed)} failed, so the "
                "interface it was built against was never implemented",
            )
            self._progress[task_id].attempts = self._max_attempts
            return False
        if waiting:
            self._defer(
                task_id,
                f"built against the declared contract of {', '.join(waiting)}; "
                "waits for them to commit",
                providers=True,
            )
            return False
        return True

    def _providers_pending(self, task_id: str) -> bool:
        """Whether a soft provider of ``task_id`` has neither committed nor failed."""
        dag = self._require_scheduler().dag
        return any(
            not dag.is_complete(dep) and dep not in self._failed
            for dep in dag.soft_dependencies(task_id)
        )

    def _contracts_hold(self, task_id: str, staged: list[NodeId]) -> bool:
        """Check the provider side: a contracted node must match its contract."""
        task = self._dag_task(task_id)
        for node_id, text in task.contract.items():
            fragment = self._node_store.get_staged(node_id)
            if fragment is None:
                continue
            reason = implementation_mismatch(text, fragment.source)
            if reason is None:
                continue
            self._log(
                EventType.CONTRACT_VIOLATION, task_id=task_id,
                node_id=str(node_id), reason=reason,
            )
            self._send_back(
                task_id, staged,
                note=(
                    f"'{node_id}' must implement its declared contract exactly — "
                    f"other tasks are being built against it: {reason}. Keep the "
                    "declared name, parameters (names, order, annotations, "
                    "defaults) and return annotation."
                ),
                kind="contract_violation",
                reason=f"'{node_id}' does not match its declared contract: {reason}",
            )
            return False
        return True

    def _add_contracts(self, task: SubTask, context: dict[str, str]) -> list[str]:
        """Layer 0: the declared contracts this task implements or builds on."""
        plan = self._require_scheduler().dag.tasks
        added: list[str] = []
        for node_id, text in visible_contracts(task, plan).items():
            key = f"{CONTRACT_PREFIX}:{node_id}"
            context[key] = render_contract(node_id, text, own=node_id in task.contract)
            added.append(key)
        return added

    def _contract_definitions(
        self, task_id: str, own: dict[str, str]
    ) -> dict[str, str]:
        """Contract stubs a task's calls are checked against at its commit."""
        task = self._dag_task(task_id)
        plan = self._require_scheduler().dag.tasks
        stubs: dict[str, str] = {}
        for node_id, text in visible_contracts(task, plan).items():
            if node_id in task.contract or str(node_id) in own:
                continue
            try:
                stubs[str(node_id)] = contract_stub(text)
            except ContractError:
                continue  # a contract the planner never validated is no authority
        return stubs

    def _contract_covered(self, task_id: str, stale: StaleRead) -> bool:
        """Whether a stale node is exactly the contract the task was built against."""
        task = self._dag_task(task_id)
        text = visible_contracts(task, self._require_scheduler().dag.tasks).get(
            stale.node_id
        )
        if text is None or stale.current_source is None:
            return False
        return implementation_mismatch(text, stale.current_source) is None

    # -- keyed registrars and interface enforcement (Wave 20, P5/P2) ---------

    def _send_back(
        self, task_id: str, staged: list[NodeId], note: str, kind: str, reason: str
    ) -> None:
        """Roll the attempt back and re-dispatch it with a kernel note.

        For the cases where the *kernel* declined the commit for a reason the
        agent can act on: the note replaces the generic retry text, and
        ``error_kind`` records which case it was.
        """
        progress = self._progress[task_id]
        progress.kernel_note = note
        progress.error_kind = kind
        self._record_failure(task_id, reason)
        for node_id in staged:
            self._node_store.rollback_node(node_id)

    def _reconcile_registrars(self, task_id: str, staged: list[NodeId]) -> bool:
        """Replay keyed-registrar appends onto the current table (no lost update).

        Only targets held INTENT_WRITE are touched. A pure keyed append is
        merged onto whatever the table holds now; anything else must upgrade to
        an exclusive WRITE, and waits when it cannot — or is sent back when the
        table moved since the agent read it, which an overwrite would undo.
        """
        for node_id in staged:
            if self._granted_mode(task_id, node_id) is not LockMode.INTENT_WRITE:
                continue
            fragment = self._node_store.get_staged(node_id)
            if fragment is None:
                continue
            mark = self._read_sets.get(task_id, {}).get(node_id)
            current = self._node_source(node_id)
            plan = plan_merge(
                mark.source if mark else None, fragment.source, current
            )
            if plan.kind is MergeKind.MERGED and plan.source is not None:
                self._stage_merge(task_id, node_id, plan.source, plan.appended, mark)
                continue
            if not self._upgrade_registrar(task_id, staged, node_id, plan.reason):
                return False
        return True

    def _stage_merge(
        self,
        task_id: str,
        node_id: NodeId,
        merged: str,
        appended: tuple[object, ...],
        mark: ReadMark | None,
    ) -> None:
        """Stage the merged table in place of the agent's copy and log it."""
        fragment = self._node_store.get_staged(node_id)
        if fragment is not None and fragment.source != merged:
            self._node_store.put_node(node_id, replace(fragment, source=merged))
        current = self._committed_fragment(node_id)
        self._log(
            EventType.REGISTRY_MERGED,
            task_id=task_id,
            node_id=str(node_id),
            appended=[getattr(e, "text", str(e)) for e in appended],
            read_version=mark.version if mark else None,
            current_version=current.version if current else None,
        )

    def _upgrade_registrar(
        self, task_id: str, staged: list[NodeId], node_id: NodeId, why: str
    ) -> bool:
        """Take a registrar exclusively for a non-append edit, or wait for it."""
        if not self._lock_table.try_acquire_all([(node_id, LockMode.WRITE)], task_id):
            self._defer(
                task_id,
                f"'{node_id}' is being appended to concurrently and this edit "
                f"needs it exclusively ({why})",
            )
            return False
        self._granted.setdefault(task_id, {})[node_id] = LockMode.WRITE
        mark = self._read_sets.get(task_id, {}).get(node_id)
        current = self._committed_fragment(node_id)
        current_digest = source_digest(current.source) if current else None
        if mark is not None and mark.digest == current_digest:
            return True
        self._send_back(
            task_id, staged,
            note=(
                f"'{node_id}' gained entries from other tasks while you worked, "
                "and your version would have overwritten them. Your bundle now "
                "holds its current content: make your change on top of it."
            ),
            kind="stale_read",
            reason=(
                f"'{node_id}' changed since it was read; an overwrite would lose "
                "entries"
            ),
        )
        return False

    def _interfaces_are_granted(self, task_id: str, staged: list[NodeId]) -> bool:
        """Hold every interface change to the task's declaration and its locks.

        A task that declared ``changes_api=False`` promised a body-only edit —
        callers' tasks ran beside it on the strength of that — so an interface
        change is refused. Any other interface change needs WRITE on
        ``node#api``; one the task was not granted is taken now if it is free,
        and waits when a concurrent task is building against the interface.
        """
        if not self._config.semantic.api_locks:
            return True
        task = self._dag_task(task_id)
        for node_id in staged:
            change = self._interface_change(node_id)
            if change is None:
                continue
            if task.changes_api is False:
                self._send_back(
                    task_id, staged,
                    note=(
                        "This task was planned as a body-only change, so other "
                        f"tasks are relying on the interface of '{node_id}' "
                        f"staying exactly as it is. Your version changed it "
                        f"({change}). Keep every signature, parameter, return "
                        "annotation, decorator, class field and import exactly "
                        "as they are; change only function bodies."
                    ),
                    kind="undeclared_api_change",
                    reason=f"body-only task changed the interface of '{node_id}'",
                )
                self._log(
                    EventType.API_ESCALATED, task_id=task_id, node_id=str(node_id),
                    outcome="refused_promise", change=change,
                )
                return False
            if not self._hold_interface(task_id, staged, node_id, change):
                return False
        return True

    def _interface_change(self, node_id: NodeId) -> str | None:
        """Describe how the staged source changes a node's interface, or None.

        A node that does not exist yet has no interface to change — nothing can
        have been built against it — and a node that only *gained* a binding
        (a new import, a new helper) broke nobody.
        """
        fragment = self._node_store.get_staged(node_id)
        old = self._dependency_source(node_id)
        if fragment is None or old is None:
            return None
        changed = changed_bindings(old, fragment.source)
        if changed == set():
            return None
        return _describe_interface_change(
            api_fingerprint(old), api_fingerprint(fragment.source), changed
        )

    def _hold_interface(
        self, task_id: str, staged: list[NodeId], node_id: NodeId, change: str
    ) -> bool:
        """Make sure the task holds WRITE on every interface it is changing."""
        resources = [
            api_resource(n)
            for n in (node_id, *self._file_fragment_ids(node_id))
        ]
        needed = [
            (r, LockMode.WRITE) for r in resources
            if not self._lock_table.holds_all([(r, LockMode.WRITE)], task_id)
        ]
        if not needed:
            return True
        if self._lock_table.try_acquire_all(needed, task_id):
            self._granted.setdefault(task_id, {}).update(
                {r: LockMode.WRITE for r, _ in needed}
            )
            self._log(
                EventType.API_ESCALATED, task_id=task_id, node_id=str(node_id),
                outcome="acquired", change=change,
            )
            return True
        readers = sorted({
            entry.holder
            for resource, _ in needed
            for entry in self._lock_table.all_entries().get(resource, [])
            if entry.holder != task_id
        })
        self._log(
            EventType.API_ESCALATED, task_id=task_id, node_id=str(node_id),
            outcome="refused_contended", change=change, readers=readers,
        )
        # The readers will validate their own commits against whatever this
        # one leaves behind; this commit only has to wait until none of them
        # is still building against the old interface.
        self._defer(
            task_id,
            f"undeclared interface change to '{node_id}' waits for "
            f"{', '.join(readers)}, which depend on its current interface",
        )
        return False

    # -- stale-read validation (Wave 20, P1/D1/R1) --------------------------

    def _reads_are_current(self, task_id: str, staged: list[NodeId]) -> bool:
        """Validate the task's read set; roll the attempt back when it fails.

        Backward validation from optimistic concurrency control: every node the
        bundle carried is compared, by digest, with what is committed *now*.
        Nothing changed → the task saw a consistent snapshot. Something changed
        → each stale node is classified and the ``semantic.stale_read`` policy
        decides; every one of them is logged with its verdict. Returns whether
        the commit may proceed.
        """
        stale = self._stale_reads_of(task_id, staged)
        if not stale:
            return True
        self._stale_reads += len(stale)
        covered = [s for s in stale if self._contract_covered(task_id, s)]
        rest = [s for s in stale if s not in covered]
        decision = decide(
            self._config.semantic.stale_read,
            rest,
            recheck=lambda nodes: self._recheck_against_current(staged, nodes),
            adjudicate=self._adjudicator(staged),
        )
        decision = StaleDecision(
            decision.verdict,
            (
                *(
                    NodeDecision(
                        s, Verdict.ACCEPT,
                        "matches the declared contract the task was built against",
                    )
                    for s in covered
                ),
                *decision.nodes,
            ),
        )
        self._log_stale_decision(task_id, decision)
        if decision.verdict is Verdict.ACCEPT:
            return True
        reasons = [
            f"stale read of '{d.stale.node_id}' ({d.stale.kind}): {d.reason}"
            for d in decision.nodes
            if d.verdict is not Verdict.ACCEPT
        ]
        if decision.verdict is Verdict.REJECT:
            self._reject(task_id, staged, reasons)
            return False
        self._stale_redispatches += 1
        progress = self._progress[task_id]
        progress.kernel_note = retry_note(decision)
        progress.error_kind = "stale_read"
        self._record_failure(task_id, "; ".join(reasons))
        for node_id in staged:
            self._node_store.rollback_node(node_id)
        return False

    def _stale_reads_of(self, task_id: str, staged: list[NodeId]) -> list[StaleRead]:
        """Every node in the task's read set whose committed content moved."""
        read_set = self._read_sets.get(task_id)
        if not read_set:
            return []
        exempt = self._stale_exempt(task_id, staged)
        own = self._staged_sources(staged)
        stale: list[StaleRead] = []
        for node_id, mark in read_set.items():
            if node_id in exempt:
                continue
            version, source, digest = self._current_for_mark(mark)
            if digest == mark.digest:
                continue
            stale.append(classify(mark, version, source, own))
        return stale

    def _stale_exempt(self, task_id: str, staged: list[NodeId]) -> set[NodeId]:
        """Read-set nodes whose staleness is already settled for this task.

        Its own WRITE-held targets cannot have changed — nobody else can commit
        them. Its INTENT_WRITE-held targets are keyed registrars that other
        tasks append to concurrently *by design*; ``_reconcile_registrars`` has
        already replayed this task's append onto whatever they hold now (or sent
        the attempt back), so a newer version there is the expected state, not
        a stale read.
        """
        granted = self._granted.get(task_id, {})
        targets = set(self._progress[task_id].target_nodes)
        return {
            node_id
            for node_id, mode in granted.items()
            if node_id in targets
            and mode in (LockMode.WRITE, LockMode.INTENT_WRITE)
        }

    def _current_for_mark(
        self, mark: ReadMark
    ) -> tuple[int | None, str | None, str | None]:
        """Return ``(version, source, digest)`` of what a read-set node is *now*.

        A fragment superseded by a whole-file commit is not gone: its symbol is
        looked up in the whole-file node, so a sibling task's whole-file rewrite
        is compared symbol-for-symbol rather than reported as a deletion.
        """
        fragment = self._committed_fragment(mark.node_id)
        if fragment is not None:
            return fragment.version, fragment.source, source_digest(fragment.source)
        parts = str(mark.node_id).split("::")
        if len(parts) >= 3:
            whole = self._committed_fragment(NodeId(parts[0]))
            if whole is not None:
                qualname = parts[2].split("#", 1)[0]
                extracted = symbol_source(whole.source, qualname)
                if extracted is not None:
                    return whole.version, extracted, source_digest(extracted)
        return None, None, None

    def _staged_sources(self, staged: list[NodeId]) -> dict[NodeId, str]:
        """Return the task's pending sources, keyed by node id."""
        own: dict[NodeId, str] = {}
        for node_id in staged:
            fragment = self._node_store.get_staged(node_id)
            if fragment is not None:
                own[node_id] = fragment.source
        return own

    def _recheck_against_current(
        self, staged: list[NodeId], stale: list[StaleRead]
    ) -> list[str]:
        """Re-run the static checks for ``staged`` against the *current* code.

        The signature check takes the changed nodes' current sources as the
        definition authority; the cross-module checks judge the task's files as
        they would be committed, against the store as it stands.
        """
        own = {str(k): v for k, v in self._staged_sources(staged).items()}
        definitions = {
            str(s.node_id): s.current_source
            for s in stale
            if s.current_source is not None
        }
        reasons: list[str] = []
        if definitions and own:
            report = self._conflict_detector.detect(
                EditRound(definitions={**definitions, **own}, callers=own)
            )
            reasons.extend(report.reasons)
        reasons.extend(self._prospective_defects(staged))
        return reasons

    def _prospective_defects(self, staged: list[NodeId]) -> list[str]:
        """Cross-module defects the task's files would have if committed now."""
        staged_set = set(staged)
        files = sorted({_file_of(str(n)) for n in staged})
        sources = StoreSources(self._node_store)
        overrides: dict[str, str | None] = {}
        for file_path in files:
            try:
                overrides[file_path] = self._assemble_preview(file_path, staged_set)
            except (SyntaxError, NodeStoreError):
                continue
        view = sources.with_overrides(overrides)
        return [
            defect.detail
            for defect in check_cross_module_api(view, frozenset(files))
        ]

    def _adjudicator(
        self, staged: list[NodeId]
    ) -> Callable[[StaleRead], bool | None] | None:
        """Bind the optional LLM adjudicator to this commit's staged code."""
        fn = self._adjudicate_fn
        if fn is None:
            return None
        own = self._staged_sources(staged)
        return lambda stale: fn(stale, own)

    def _log_stale_decision(self, task_id: str, decision: StaleDecision) -> None:
        """Log one ``STALE_READ`` event per stale node, each with its verdict."""
        attempt = self._progress[task_id].attempts
        for node in decision.nodes:
            self._log(
                EventType.STALE_READ,
                task_id=task_id,
                attempt=attempt,
                node_id=str(node.stale.node_id),
                layer=node.stale.mark.layer,
                read_version=node.stale.mark.version,
                current_version=node.stale.current_version,
                change=str(node.stale.kind),
                referenced=node.stale.referenced,
                verdict=str(node.verdict),
                commit_verdict=str(decision.verdict),
                policy=self._config.semantic.stale_read,
                reason=node.reason,
            )

    def _reject(self, task_id: str, staged: list[NodeId], reasons: list[str]) -> None:
        """Log a rejection and discard the staged (pending) fragments."""
        self._conflict_rejections += 1
        self._log(EventType.CONFLICT_DETECTED, task_id=task_id, reasons=reasons)
        if reasons:
            self._record_failure(task_id, "; ".join(reasons))
        for node_id in staged:
            self._node_store.rollback_node(node_id)

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
        self,
        staged: list[NodeId],
        peers: dict[str, str] | None = None,
        *,
        task_id: str | None = None,
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
        contracts = (
            self._contract_definitions(task_id, own) if task_id is not None else {}
        )
        definitions = {**contracts, **peers, **own}
        same_file = {
            k: v for k, v in definitions.items() if _file_of(k) in own_files
        }
        headers = {k: v for k, v in same_file.items() if _is_header_id(k)}
        previous = {
            k: source
            for k in own
            if (source := self._node_source(NodeId(k))) is not None
        }
        # Each staged source is both a definition authority and a caller, so the
        # detector validates this task's new calls against every new signature.
        return EditRound(
            definitions=definitions,
            callers=own,
            header_edits=headers,
            symbol_edits=same_file,
            registry_edits=own,
            previous=previous,
        )

    def _safe_output_path(self, file_path: str) -> Path:
        """Resolve a file this wave may write, refusing anything outside the tree.

        The last gate before content reaches the filesystem. ``parse_plan``
        already refuses an escaping target, but it is not the only way a plan
        arrives: ``install_plan`` is called directly by the interactive app and
        by every cascade wave, and neither goes through the planner's parser. A
        guard that only one of three entry points passes through is not a guard.

        Resolution (not just the lexical check) because this is the layer that
        can see the filesystem: a ``vendor/`` symlink pointing at ``/etc`` is
        invisible to a string check and obvious to ``resolve()``.
        """
        check_node_id(file_path, mak_dir_name=self._mak_dir.name or _MAK_DIR_NAME)
        resolved = safe_path_under(self._work_dir, file_path, label="output file")
        if self._is_store_path(resolved):
            raise UnsafeNodeIdError(
                f"refusing output file '{file_path}': it resolves inside MAK's own "
                f"{self._mak_dir} directory, which is never project source"
            )
        return resolved

    def _reconstruct_affected(self, nodes: list[NodeId]) -> list[str]:
        """Rewrite each file touched by ``nodes`` from its committed fragments.

        The *non-transactional* materialization path: bringing an already-committed
        file back into agreement with the store, as ``_accept_noop`` does when a
        node's on-disk file pre-dates its committed version. The edit path does
        not come through here — it goes through ``install_files``, which journals
        what it is about to overwrite. Nothing is committed by this call, so
        there is nothing for a journal to roll back; every write is still atomic.
        """
        files, contents, destinations = render_affected(
            self._node_store, nodes, self._safe_output_path
        )
        for file_path in files:
            write_text_atomic(destinations[file_path], contents[file_path])
            self._node_store.record_materialized(file_path, contents[file_path])
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
                EventType.TASK_FAILED,
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
        if progress.kernel_note is not None:
            note, progress.kernel_note = progress.kernel_note, None
            return note
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
        # A re-dispatch replaced those tasks' read sets; persist them now rather
        # than at the next unrelated state transition.
        self._require_scheduler().save()

    def _enrich_bundle(self, bundle: TaskBundle) -> _Dispatch:
        """Attach every layer of context, record what was attached, and gate it.

        Layer 0 (Wave 20) is ``contract:<id>`` — every declared contract the
        task implements or builds on (its own, its providers', its context's),
        so a dependent is shown the fixed interface even before its provider's
        code exists. Then five layers, each only adding entries not present:

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
        self._record_grant(task)
        bundle = self._resume_bundle(bundle)
        context = dict(bundle.context)
        target_files = {str(n).split("::", 1)[0] for n in bundle.target_nodes}
        layers: dict[str, list[str]] = {}
        layers["contract"] = self._add_contracts(task, context)
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
        self._record_read_set(task, context, layers)
        return self._gate_dispatch(
            task,
            replace(bundle, context=context),
            layers,
            cross_file_dropped=dropped,
        )

    def _resume_bundle(self, bundle: TaskBundle) -> TaskBundle:
        """Narrow a re-queued task to its open grants and attach the kernel note.

        A task the scheduler re-queues (a released parked result, a deadlock
        victim) is dispatched from its full ``SubTask``: without this it would
        redo grants it already committed, and arrive with no word of why.
        """
        progress = self._progress.get(bundle.task_id)
        if progress is None:
            return bundle
        if progress.completed_nodes:
            bundle = replace(bundle, target_nodes=progress.remaining)
        if bundle.retry_note is None and progress.kernel_note is not None:
            bundle = replace(bundle, retry_note=progress.kernel_note)
            progress.kernel_note = None
        return bundle

    def _record_read_set(
        self, task: SubTask, context: dict[str, str], layers: dict[str, list[str]]
    ) -> None:
        """Record every node this bundle carries, at the version it carries.

        Called on the dispatching thread straight after enrichment. Commits
        happen on that same thread, so nothing can advance the store between
        the sources being read into ``context`` and the stamps taken here.
        """
        absent = [
            node_id
            for node_id in task.context_nodes
            if self._dependency_source(node_id) is None
        ]
        read_set = build_read_set(
            context,
            layers,
            absent,
            fetch=self._committed_fragment,
            expand=self._file_fragment_ids,
        )
        self._read_sets[task.task_id] = read_set
        scheduler = self._scheduler
        if scheduler is not None:
            persisted = scheduler.annotations.setdefault("read_sets", {})
            if isinstance(persisted, dict):
                persisted[task.task_id] = read_set_to_json(read_set)

    def _committed_fragment(self, node_id: NodeId) -> NodeFragment | None:
        """Return a node's committed fragment, or None when it has none."""
        try:
            return self._node_store.get_node(node_id)
        except NodeStoreError:
            return None

    def _file_fragment_ids(self, node_id: NodeId) -> list[NodeId]:
        """Return the committed fragments of a bare whole-file id (else none)."""
        if "::" in str(node_id):
            return []
        return [n for n in self._node_store.list_nodes(str(node_id)) if n != node_id]

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
        """Layer 2: the nodes the planner explicitly listed as context.

        A whole-file id whose file is stored as fragments has no node of its
        own, so it is assembled from them — the planner asked for the file, and
        shipping nothing for it (as a plain ``get_node`` lookup did) left the
        agent blind to exactly what it was told to read.
        """
        added: list[str] = []
        for node_id in context_nodes:
            source = self._dependency_source(node_id)
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
        candidates = self._scan_for_symbols(symbols, target_files, context)
        return self._spend_cross_file_budget(candidates, context, budget)

    def _scan_for_symbols(
        self,
        symbols: set[str],
        target_files: set[str],
        context: dict[str, str],
    ) -> list[tuple[NodeId, str, frozenset[str]]]:
        r"""Each node mentioning one of ``symbols``, with the symbols it hit.

        Looked up in the per-wave inverted index rather than by regex-scanning
        every node's source. The old pass walked the whole store and ran a
        findall over each node on **every dispatch and every retry** — the byte
        budget caps what is *sent*, not what is scanned, so on a large repo with
        a wide plan this was the dominant cost of enrichment.

        The result is unchanged, node for node and byte for byte: the index keys
        a node under exactly the maximal ``\w+`` runs in its source, which is the
        same condition ``\bsymbol\b`` tests. A symbol that is not a plain
        identifier cannot be answered that way and falls back to the scan.
        """
        exotic = {s for s in symbols if not _WORD.fullmatch(s)}
        hits_by_node: dict[NodeId, set[str]] = {}
        if exotic:
            pattern = re.compile(
                r"\b(?:" + "|".join(re.escape(s) for s in sorted(exotic)) + r")\b"
            )
        index = self._symbol_index()
        for symbol in symbols - exotic:
            for node_id in index.get(symbol, ()):
                hits_by_node.setdefault(node_id, set()).add(symbol)

        found: list[tuple[NodeId, str, frozenset[str]]] = []
        for xfile_id in self._node_store.list_nodes():
            if str(xfile_id).split("::", 1)[0] in target_files:
                continue  # same-file already handled in layer 3
            if _context_has(context, xfile_id):
                continue
            hits = set(hits_by_node.get(xfile_id, ()))
            if exotic:
                source = self._node_source(xfile_id)
                if source:
                    hits |= set(pattern.findall(source))
            if not hits:
                continue
            source = self._node_source(xfile_id)
            if not source:
                continue
            found.append((xfile_id, source, frozenset(hits)))
        return found

    def _symbol_index(self) -> dict[str, list[NodeId]]:
        """Return the ``symbol -> node ids`` index, rebuilt when the store moves.

        Keyed on ``NodeStore.generation``, which changes exactly when the
        committed set does — so one build serves every dispatch and retry of a
        wave, and a commit mid-wave invalidates it without the session having to
        know which nodes moved.
        """
        generation = self._node_store.generation
        if self._symbol_index_at != generation:
            self._symbol_index_cache = self._build_symbol_index()
            self._symbol_index_at = generation
        return self._symbol_index_cache

    def _build_symbol_index(self) -> dict[str, list[NodeId]]:
        """Index every committed node under each identifier its source contains."""
        index: dict[str, list[NodeId]] = {}
        for node_id in self._node_store.list_nodes():
            source = self._node_source(node_id)
            if not source:
                continue
            for token in set(_WORD.findall(source)):
                index.setdefault(token, []).append(node_id)
        return index

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
        dag = self._require_scheduler().dag
        for dep_id in sorted(task.depends_on):
            if dep_id in dag.soft_dependencies(task.task_id) and not dag.is_complete(
                dep_id
            ):
                continue  # not written yet: its declared contract is layer 0
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
        # order_key: when several fragments fold into one whole-file grant they
        # are concatenated, and concatenating them in the order the model
        # happened to emit puts imports after code. That still compiles, so every
        # downstream gate passes it — the store's own source order is the
        # authority, and the header leads regardless.
        accepted, dropped = map_returned_sources(
            grant, new_sources, order_key=self._node_store.node_order
        )
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
        self._lock_table.release(
            node_id, self._granted_mode(task_id, node_id), task_id
        )

    def _granted_mode(self, task_id: str, node_id: NodeId) -> LockMode:
        """Return the mode ``task_id`` was granted on a target (WRITE if unknown)."""
        return self._granted.get(task_id, {}).get(node_id, LockMode.WRITE)

    def _record_grant(self, task: SubTask) -> None:
        """Remember the lock set a task was dispatched under."""
        self._granted[task.task_id] = dict(lock_requests(task, self._lock_policy))

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
            # The project lease is renewed on the same tick as the task leases:
            # both answer "is this session still alive?", and a run that renews
            # one but not the other can be reported as abandoned mid-wave.
            if self._project_lease is not None:
                self._project_lease.heartbeat()
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
            (task.task_id, node_id, mode)
            for task in scheduler.ready_queue
            for node_id, mode in lock_requests(task, self._lock_policy)
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

    def teardown(self, execution: ExecutionResult | None = None) -> TeardownResult:
        """Run the test suite and decide, honestly, whether to push.

        Returns a :class:`~mak.teardown.TeardownResult` rather than a bool. The
        bool started at ``True`` and stayed there when no test runner was
        configured, so a project with no ``test_command`` reported "tests passed"
        and — with ``auto_push`` on — pushed, every run. It also never looked at
        the run: failed, blocked, and skipped tasks all pushed too.

        Both are now gates. The suite's outcome is one of four
        (:class:`~mak.teardown.SuiteOutcome`), a runner that raises is an ``ERROR``
        rather than an unnoticed pass, and the push additionally requires the
        **aggregate** execution outcome to be satisfied — ``execution`` when the
        caller has one (it knows about cascade waves; the session does not), else
        this session's own last result.
        """
        outcome, output = self._run_tests()
        aggregate = execution or ExecutionResult(
            initial=self._last_result or self._empty_result()
        )
        pushed, skip_reason = self._maybe_push(outcome, aggregate)
        result = TeardownResult(
            outcome=outcome,
            output=output,
            pushed=pushed,
            push_skipped_reason=skip_reason,
        )
        self._log(
            EventType.SESSION_ENDED,
            test_outcome=str(outcome),
            tests_passed=outcome is SuiteOutcome.PASSED,
            pushed=pushed,
            push_skipped=skip_reason,
            completed=len(self._completed),
            failed=len(self._failed),
            output=output[:500],
        )
        return result

    def _run_tests(self) -> tuple[SuiteOutcome, str]:
        """Run the configured suite, if there is one, and classify the result."""
        if self._test_runner is None:
            return SuiteOutcome.SKIPPED, "no test_command configured; nothing ran"
        try:
            passed, output = self._test_runner()
        except Exception as exc:  # noqa: BLE001 - any runner defect is an outcome
            # Deliberately not re-raised: teardown's job is to report, and a
            # runner that blew up is a report, not a reason to lose the run's
            # results. It is an ERROR, never a pass — which is what the TUI's
            # warning-and-carry-on used to turn it into.
            self._log(EventType.SESSION_ENDED, test_runner_error=str(exc))
            return SuiteOutcome.ERROR, f"the test runner raised: {exc}"
        return (SuiteOutcome.PASSED if passed else SuiteOutcome.FAILED), output

    def _maybe_push(
        self, outcome: SuiteOutcome, execution: ExecutionResult
    ) -> tuple[bool, str | None]:
        """Apply the push gate; return ``(pushed, why_not)``."""
        if not self._config.git.auto_push or self._git is None:
            return False, None
        if not execution.request_satisfied:
            return False, (
                "the run did not fully succeed "
                f"({execution.summary_line()}); nothing was pushed"
            )
        if not may_push(outcome, self._config.session.test_policy):
            return False, (
                f"tests {outcome.value} and session.test_policy is "
                f"'{self._config.session.test_policy}'; nothing was pushed"
            )
        self._git.push()
        return True, None

    @staticmethod
    def _empty_result() -> SessionResult:
        """Stand in for a session that never ran, so teardown has a verdict."""
        return SessionResult(state=SessionState.CREATED, completed=(), failed=())

    # -- crash recovery ----------------------------------------------------

    def recover(self) -> int:
        """Expire stale leases and re-queue incomplete tasks from disk.

        Returns the number of leases expired. Must be called before ``run`` when
        resuming a crashed session; rebuilds the scheduler from ``task_graph.json``
        if one is present.

        A task graph that cannot be read leaves the session un-planned rather
        than raising, so the caller reports "nothing to recover" and the operator
        can start a fresh run. Raising instead would break ``--recover`` on
        precisely the crash it exists to handle: a kill mid-write is what
        truncates that file in the first place.
        """
        # Same ordering rule as ``initialize``: ownership, then any commit the
        # crash left in flight, and only then the state that depends on both.
        self._acquire_project()
        self._recover_journal()
        expired = self._lock_table.expire_stale()
        graph_path = self._mak_dir / "task_graph.json"
        if graph_path.exists():
            try:
                scheduler = Scheduler.from_persisted(
                    graph_path,
                    self._lock_table,
                    self._runner(),
                    self._registry,
                    max_concurrent=self._max_concurrent,
                )
            except SchedulingError as exc:
                self._log(
                    EventType.SESSION_ENDED,
                    recover_failed=True,
                    task_graph=str(graph_path),
                    reason=str(exc),
                )
                print(
                    f"mak: the saved task graph at {graph_path} could not be read "
                    f"({exc}); there is nothing to resume.",
                    file=sys.stderr,
                )
                return len(expired)
            self._scheduler = scheduler
            self._wave_graph = dep_graph_from_store(self._node_store)
            self._lock_policy = build_lock_policy(
                self._config.semantic,
                self._node_store,
                self._wave_graph,
                list(scheduler.dag.tasks.values()),
            )
            scheduler.use_lock_policy(self._lock_policy)
            self._granted = {}
            persisted = scheduler.annotations.get("read_sets", {})
            self._read_sets = {
                str(task_id): read_set_from_json(raw)
                for task_id, raw in (
                    persisted.items() if isinstance(persisted, dict) else ()
                )
            }
            self._progress = {
                t.task_id: self._restore_progress(scheduler, t)
                for t in scheduler.dag.tasks.values()
            }
            # A resumed wave inherits whatever the crashed one had already
            # written, which is the correct starting inventory for it: those
            # files do exist now, and a no-op about them is answerable.
            self._preexisting_files = {
                str(node_id).split("::", 1)[0]
                for node_id in self._node_store.list_nodes()
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
        """Return fix-up tasks for everything this wave left broken between tasks.

        Three sources, one review flow:

        - **cross-module defects** (:meth:`detect_cross_module_defects`) — code
          the wave wrote that contradicts the code it uses: unresolved imports
          and attributes, wrong arity or constructor arguments, broken
          overrides, new import cycles, duplicated implementations;
        - **cascade** (Wave 20, R3) — callers of every symbol whose signature
          changed or which was deleted/renamed this wave, found on the real
          reference graph (before and after the wave), same-file callers
          included, minus callers whose calls already fit the new signature;
        - **gates** — whatever the optional type-check, impact-test and
          import-smoke gates found, when a project turns them on.

        A caller named by more than one is given one task, not several. Returns
        an empty list when nothing is broken — the expected outcome when the
        planner was thorough. Idempotent for an unchanged store (the cascade
        loop asks twice).
        """
        fixes = self._cross_module_fix_tasks(self.detect_cross_module_defects())
        cascade = self._cascade_fix_tasks(self._cascade_items())
        gated = self._gate_fix_tasks(self._gate_findings())
        return _merge_fixups(_merge_fixups(fixes, cascade), gated)

    # -- cascade on the real graph (Wave 20, R3) -----------------------------

    def _cascade_items(self) -> list[CascadeItem]:
        """Callers of symbols this wave re-signed or deleted, on the real graph.

        Cached per store generation and logged once per finding
        (``CONFLICT_DETECTED`` with ``kind="cascade"``), like the cross-module
        defects: the finding is on the record even if the fix-up is declined.
        """
        generation = self._node_store.generation
        if self._cascade_at is not None and self._cascade_at[0] == generation:
            return list(self._cascade_at[1])
        items = self._find_cascade_items()
        for item in items:
            self._log(
                EventType.CONFLICT_DETECTED,
                kind="cascade",
                file=_file_of(str(item.caller)),
                defining_file=item.change.file,
                reasons=[_describe_cascade(item).splitlines()[0]],
            )
        self._cascade_at = (generation, list(items))
        return items

    def _find_cascade_items(self) -> list[CascadeItem]:
        """Diff this wave's symbols and walk the reference graph for callers."""
        scope = sorted(self._wave_file_before)
        if not scope:
            return []
        changes = [
            change
            for file_path in scope
            for change in diff_symbols(
                file_path,
                self._wave_file_before.get(file_path) or "",
                self._file_source_or_empty(file_path),
            )
        ]
        if not any(c.kind is not SymbolChangeKind.BODY for c in changes):
            return []
        return cascade_items(
            changes,
            self._wave_graph,
            dep_graph_from_store(self._node_store),
            source_of=self._node_source,
            after_sources=lambda f: self._file_source_or_empty(f) or None,
        )

    def _cascade_fix_tasks(self, items: list[CascadeItem]) -> list[SubTask]:
        """One fix-up task per broken caller, naming every change it must absorb."""
        by_caller: dict[NodeId, list[CascadeItem]] = {}
        for item in items:
            by_caller.setdefault(item.caller, []).append(item)
        tasks: list[SubTask] = []
        for caller, found in sorted(by_caller.items()):
            symbols = sorted({i.change.old.qualname for i in found})
            context = sorted({
                d for i in found for d in i.definers
                if self._node_source(d) is not None and d != caller
            })
            for item in found:
                if not any(_file_of(str(c)) == item.change.file for c in context):
                    context.extend(self._node_store.list_nodes(item.change.file))
            tasks.append(SubTask(
                task_id=_fixup_task_id("cascade", f"{'_'.join(symbols)}_{caller}"),
                description=(
                    f"Update `{caller}` for changes this wave made to code it "
                    "uses:\n"
                    + "\n".join(_describe_cascade(i) for i in found)
                    + "\nAdjust every use in this node to match — do not restore "
                    "the old definitions or add compatibility shims."
                ),
                target_nodes=[caller],
                context_nodes=list(dict.fromkeys(n for n in context if n != caller)),
                depends_on=[],
                agent_type=self._default_agent_type or "",
            ))
        return tasks

    # -- optional heavy gates (Wave 20, D3/D4/D6) ----------------------------

    def _gate_findings(self) -> list[GateFinding]:
        """Run the enabled gates over this wave, once per store generation."""
        if not self._gates.enabled or not self._wave_file_before:
            return []
        generation = self._node_store.generation
        if self._gates_at is not None and self._gates_at[0] == generation:
            return list(self._gates_at[1])
        findings = self._gates.run(
            self._wave_view(),
            log=lambda **p: self._log(EventType.GATE_FINDING, **p),
        )
        for finding in findings:
            self._log(
                EventType.GATE_FINDING,
                gate=finding.gate,
                file=finding.file,
                tasks=list(finding.tasks),
                detail=finding.detail,
            )
        self._gates_at = (generation, list(findings))
        return findings

    def _wave_view(self) -> WaveView:
        """Return what the gates may see of the wave that just ran."""
        tasks = tuple(dict.fromkeys(task for task, *_ in self._wave_commit_log))
        return WaveView(
            work_dir=self._work_dir,
            before=dict(self._wave_file_before),
            current=lambda f: self._file_source_or_empty(f) or None,
            subset=self._subset_files,
            writers={f: list(w) for f, w in self._wave_file_writers.items()},
            tasks=tasks,
            task_nodes=lambda t: list(dict.fromkeys(
                node for task, node, *_ in self._wave_commit_log if task == t
            )),
            file_nodes=lambda f: self._node_store.list_nodes(f),
            timeout_s=self._config.semantic.gate_timeout_s,
            max_overlays=self._config.semantic.impact_max_overlays,
        )

    def _subset_files(self, tasks: frozenset[str]) -> dict[str, str | None]:
        """Every touched file as the pre-wave state plus only ``tasks``' commits.

        Rebuilt from fragments, not files: each touched file's pre-wave
        fragments, with the chosen tasks' committed nodes swapped in (in commit
        order) and a whole-file commit superseding the fragments before it.
        """
        files: dict[str, str | None] = {}
        for file_path in self._wave_file_before:
            nodes: dict[NodeId, tuple[int | None, str]] = {
                node: (order, source)
                for node, order, source in self._wave_fragments_before.get(
                    file_path, []
                )
            }
            for task, node, order, source in self._wave_commit_log:
                if task not in tasks or _file_of(str(node)) != file_path:
                    continue
                if "::" not in str(node):
                    nodes = {}
                nodes[node] = (order, source)
            ordered = sorted(
                nodes.items(),
                key=lambda item: (item[1][0] is None, item[1][0] or 0, str(item[0])),
            )
            files[file_path] = (
                assemble_fragments(
                    [NodeFragment(n, "fragment", src, 1) for n, (_, src) in ordered]
                )
                if ordered
                else None
            )
        return files

    def _gate_fix_tasks(self, findings: list[GateFinding]) -> list[SubTask]:
        """One fix-up task per gate and file, naming the task(s) it traces to."""
        grouped: dict[tuple[str, str], list[GateFinding]] = {}
        for finding in findings:
            if finding.targets:
                grouped.setdefault((finding.gate, finding.file), []).append(finding)
        tasks: list[SubTask] = []
        for (gate, file_path), found in sorted(grouped.items()):
            culprits = sorted({t for f in found for t in f.tasks})
            tasks.append(SubTask(
                task_id=_fixup_task_id(f"{gate}_fix", file_path),
                description=(
                    f"The {gate.replace('_', ' ')} gate found a problem this "
                    f"wave introduced (task(s) {', '.join(culprits) or 'unknown'}): "
                    + "; ".join(f.detail for f in found)
                    + ". Fix the code so the combined result is correct — do not "
                    "weaken or delete the check that caught it."
                ),
                target_nodes=list(dict.fromkeys(n for f in found for n in f.targets)),
                context_nodes=list(dict.fromkeys(n for f in found for n in f.context)),
                depends_on=[],
                agent_type=self._default_agent_type or "",
            ))
        return tasks

    # -- post-wave cross-module analysis -------------------------------------

    def detect_cross_module_defects(self) -> list[CrossModuleDefect]:
        """Report where the files this wave wrote contradict the rest of the code.

        Every gate MAK runs is scoped to one task's edit, so two tasks can each
        finish clean and still leave the codebase broken between them: a module
        that imports a name its target never defines, calls a sibling's
        function with the wrong arity, reads ``mod.name`` from a module that no
        longer binds it, constructs a class its new fields reject, overrides a
        base method it can no longer honour, closes an import cycle, or
        duplicates a function another task just wrote.

        Scope is every file with a commit this wave, judged against the store as
        it now stands — and only defects the wave **introduced** are reported:
        the same checks run over the pre-wave state, and anything they also
        find there is pre-existing debt, not this wave's fix-up work. Each
        defect is logged as ``CONFLICT_DETECTED`` so it is on the record even if
        the operator declines the fix-up wave. Cached per store generation.
        """
        scope = frozenset(self._wave_file_before) | frozenset(
            _file_of(str(n)) for n in self._wave_committed
        )
        if not scope:
            return []
        generation = self._node_store.generation
        if self._defects_at is not None and self._defects_at[0] == generation:
            return list(self._defects_at[1])
        after = StoreSources(self._node_store)
        before = after.with_overrides({
            f: self._wave_file_before.get(f)
            for f in scope
            if f in self._wave_file_before
        })
        baseline = {d.detail for d in _post_wave_checks(before, scope)}
        defects = [
            d for d in _post_wave_checks(after, scope) if d.detail not in baseline
        ]
        defects.extend(check_new_cycles(before, after, scope))
        defects.extend(check_duplicates(self._created_functions(before, after, scope)))
        for defect in defects:
            self._log(
                EventType.CONFLICT_DETECTED,
                kind=defect.kind,
                file=defect.file,
                defining_file=defect.defining_file,
                reasons=[defect.detail],
            )
        self._defects_at = (generation, list(defects))
        return defects

    def _created_functions(
        self, before: StoreSources, after: StoreSources, scope: frozenset[str]
    ) -> list[CreatedFunction]:
        """Top-level functions this wave created, with the task that wrote each."""
        created: list[CreatedFunction] = []
        for file_path in sorted(scope):
            if file_path not in after:
                continue
            old = symbol_table(before[file_path]) if file_path in before else {}
            for name, definition in symbol_table(after[file_path]).items():
                if definition.kind != "function" or name in old:
                    continue
                writer = self._writer_of(file_path, name)
                if writer is not None:
                    created.append(
                        CreatedFunction(file_path, name, definition.source, writer)
                    )
        return created

    def _writer_of(self, file_path: str, name: str) -> str | None:
        """Return the task that committed the node defining ``name``."""
        for node_id in (
            NodeId(f"{file_path}::function::{name}"),
            NodeId(file_path),
        ):
            if node_id in self._wave_node_writer:
                return self._wave_node_writer[node_id]
        writers = self._wave_file_writers.get(file_path)
        return writers[-1] if writers else None

    def _file_sources(self) -> dict[str, str]:
        """Return the assembled current source of every file the store holds."""
        return dict(StoreSources(self._node_store))

    def _file_source_or_empty(self, file_path: str) -> str:
        """Return a file's assembled committed source, or ``""`` when it has none."""
        fragments = self._node_store.get_committed_fragments(file_path)
        return assemble_fragments(fragments) if fragments else ""

    def _cross_module_fix_tasks(
        self, defects: list[CrossModuleDefect]
    ) -> list[SubTask]:
        """One fix-up task per file whose cross-module references do not resolve.

        R2: the fix-up names the tasks whose work met in the defect and carries
        the wave's diff of both files, not just the defining modules' current
        source — the agent is told what changed on each side, and by whom.
        """
        by_file: dict[str, list[CrossModuleDefect]] = {}
        for defect in defects:
            by_file.setdefault(defect.file, []).append(defect)
        tasks: list[SubTask] = []
        for file_path, found in sorted(by_file.items()):
            listed = "; ".join(d.detail for d in found)
            defining = sorted({d.defining_file for d in found} - {file_path})
            tasks.append(SubTask(
                task_id=_fixup_task_id("api_fix", file_path),
                description=(
                    f"Fix `{file_path}` so its use of "
                    f"{', '.join(f'`{d}`' for d in defining) or 'its own code'} "
                    f"matches what those modules actually define: {listed}. Use "
                    "the real names and signatures — do not add fallbacks or "
                    "try/except around the imports."
                    + self._pair_context(file_path, defining)
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

    def _pair_context(self, file_path: str, defining: list[str]) -> str:
        """Name the tasks behind a defect and show what each side changed."""
        parts: list[str] = []
        for path in [file_path, *defining]:
            writers = self._wave_file_writers.get(path)
            if not writers:
                continue
            diff = _bounded_diff(
                self._wave_file_before.get(path) or "",
                self._file_source_or_empty(path),
                path,
            )
            parts.append(
                f"\n\n`{path}` was changed this wave by task(s) "
                f"{', '.join(writers)}:\n{diff}"
            )
        return "".join(parts)

    # -- helpers -----------------------------------------------------------

    def _require_scheduler(self) -> Scheduler:
        if self._scheduler is None:
            raise SessionError("no plan installed; call plan() or install_plan() first")
        return self._scheduler


def _fixup_task_id(prefix: str, subject: str) -> str:
    """Build a collision-free task id for a generated fix-up task.

    Sanitizing to ``[a-zA-Z0-9_]`` is lossy: ``a/b.py`` and ``a-b.py`` both
    become ``a_b_py``. ``DAG`` rejects a duplicate task id outright, so two
    unrelated files whose names happen to sanitize alike took down the whole
    cascade wave — every fix-up lost to a naming coincidence. The digest is of
    the *original* subject, so ids that differ before sanitizing still differ
    after it.
    """
    slug = re.sub(r"[^a-zA-Z0-9]", "_", f"{prefix}_{subject}")
    digest = hashlib.blake2s(subject.encode("utf-8"), digest_size=4).hexdigest()
    return f"{slug}_{digest}"


def _post_wave_checks(
    sources: Mapping[str, str], scope: frozenset[str]
) -> list[CrossModuleDefect]:
    """Every whole-repository check that judges ``scope`` against ``sources``."""
    index = ModuleIndex(sources)
    return [
        *check_cross_module_api(sources, scope),
        *check_module_attributes(index, scope),
        *check_constructors(index, scope),
        *check_overrides(index, scope),
    ]


def _bounded_diff(before: str, after: str, path: str, limit: int = 3000) -> str:
    """Return a unified diff of one file's wave, truncated to ``limit`` chars."""
    text = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path} (before this wave)",
            tofile=f"{path} (now)",
        )
    )
    if len(text) > limit:
        return text[:limit] + "\n… (diff truncated)"
    return text or "(no textual change)"


def _describe_interface_change(
    before: str | None, after: str | None, changed: set[str] | None
) -> str:
    """Return a short, readable account of an interface change."""
    if before is None or after is None or changed is None:
        return "its interface could not be read"
    old, new = set(before.splitlines()), set(after.splitlines())
    removed = [line.strip() for line in before.splitlines() if line not in new]
    added = [line.strip() for line in after.splitlines() if line not in old]
    parts = []
    if removed:
        parts.append("was: " + "; ".join(removed[:3]))
    if added:
        parts.append("now: " + "; ".join(added[:3]))
    names = ", ".join(sorted(changed)[:5])
    return f"{names}: " + (" / ".join(parts) or "its interface changed")


def _describe_cascade(item: CascadeItem) -> str:
    """Return one line (plus a bounded diff) describing a change to absorb."""
    change = item.change
    where = f"`{change.old.qualname}` in `{change.file}`"
    if change.kind is SymbolChangeKind.DELETED:
        hint = (
            f" — it appears to be renamed to `{item.renamed_to}`"
            if item.renamed_to else ""
        )
        return f"- {where} was deleted this wave{hint}."
    assert change.new is not None
    diff = _bounded_diff(
        change.old.source, change.new.source, change.old.qualname, 1500
    )
    return (
        f"- {where}: signature changed from `{change.old.signature}` to "
        f"`{change.new.signature}`.\n{diff}"
    )


def _merge_fixups(fixes: list[SubTask], extra: list[SubTask]) -> list[SubTask]:
    """Fold a fix-up into an earlier one that already targets its node."""
    merged = list(fixes)
    for task in extra:
        home = next(
            (
                i for i, fix in enumerate(merged)
                if set(task.target_nodes) <= set(fix.target_nodes)
            ),
            None,
        )
        if home is None:
            merged.append(task)
            continue
        fix = merged[home]
        merged[home] = replace(
            fix,
            description=f"{fix.description}\n\nAlso: {task.description}",
            context_nodes=list(
                dict.fromkeys([*fix.context_nodes, *task.context_nodes])
            ),
        )
    return merged


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

# Maximal word runs, which is both what the symbol index is keyed on and the
# test for whether a symbol *can* be: ``\bfoo\b`` matches exactly where ``foo``
# is one such run, so indexing the runs answers the same question. Node ids yield
# Python identifiers, so every symbol qualifies in practice — the ``fullmatch``
# check exists so an id that somehow carries punctuation is scanned by the old
# regex path rather than silently missed by the index.
_WORD = re.compile(r"\w+")

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
