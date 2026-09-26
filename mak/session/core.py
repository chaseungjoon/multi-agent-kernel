"""Session: the state machine that drives init → plan → run → teardown.

A ``Session`` owns the lifecycle and nothing else. **initialize** takes the
project, resolves an interrupted commit and reconciles the store with the tree;
**plan** / **install_plan** turn a request into a scheduled wave; **run** loops
the scheduler — dispatching every lock-satisfiable ready task onto a bounded
thread pool, collecting finished results in batches and settling them; and
**teardown** runs the suite and decides whether to push. **recover** resumes a
crashed wave from ``.mak/task_graph.json``.

The work behind each phase belongs to a collaborator built once per session
(:mod:`mak.session.wiring`): enrichment (:mod:`~mak.session.dispatch`), result
settling (:mod:`~mak.session.batch`), the commit pipeline
(:mod:`~mak.session.commit`), retries and no-ops (:mod:`~mak.session.outcomes`),
post-wave analysis (:mod:`~mak.session.post_wave`) and the rest. Collaborators
receive what they need explicitly — never the session — and every piece of
per-wave state lives in one :class:`~mak.session.wave.WaveState`, replaced
whole for each wave.

The subsystems (node store, lock table, registry, agent runner, conflict
detector, planner, git helper, logger) are injected so the session is testable
with fakes and is not bound to concrete subprocess/LLM backends.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from mak.agent_runner.registry import AdapterRegistry
from mak.config import MakConfig
from mak.conflict_detector.cross_module_check import CrossModuleDefect
from mak.conflict_detector.detector import ConflictDetector
from mak.core.exceptions import SessionError
from mak.core.logging import EventType, SessionLogger
from mak.core.types import NodeId, SubTask
from mak.execution_result import ExecutionResult
from mak.git_integration.git import GitHelper
from mak.lock_manager.deadlock_detector import DeadlockDetector
from mak.lock_manager.project_lease import ProjectLease
from mak.node_store.store import NodeStore
from mak.planner.depgraph import dep_graph_from_store
from mak.planner.planner import Planner, PlannerLLM
from mak.planner.review import display_plan_for_review
from mak.planner.validation import PlanFinding
from mak.scheduler.dag import DAG
from mak.scheduler.scheduler import Scheduler
from mak.semantic.contracts import soft_edges
from mak.semantic.gate_types import ProcessRunner
from mak.semantic.locking import build_lock_policy
from mak.session.batch import BatchProcessor
from mak.session.commit.pipeline import CommitPipeline
from mak.session.concurrency import ConcurrentRunner
from mak.session.events import EventLog
from mak.session.post_wave import PostWaveAnalyzer
from mak.session.results import wave_result
from mak.session.types import (
    Assigner,
    LockTableLike,
    PlanProposal,
    SessionResult,
    SessionState,
    SubTaskProgress,
    TestRunner,
)
from mak.session.usage import combined_usage, total_tokens
from mak.session.wave import WaveState
from mak.session.wiring import SessionInputs, wire
from mak.teardown import TeardownResult


class Session:
    """Orchestrates init → plan → run → teardown over injected subsystems."""

    def __init__(
        self,
        *,
        session_id: str,
        config: MakConfig,
        node_store: NodeStore,
        lock_table: LockTableLike,
        registry: AdapterRegistry,
        agent_runner: Assigner,
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
        self._node_store = node_store
        self._lock_table = lock_table
        self._registry = registry
        self._agent_runner = agent_runner
        self._planner = planner
        self._git = git_helper
        self._log = EventLog(logger, session_id)
        # Exclusive ownership of this project's state, taken before anything
        # reads or mutates it. ``None`` leaves a session unguarded, which is
        # what the tests that build one directly want; every real front end
        # supplies one.
        self._project_lease = project_lease
        self._parts = wire(
            SessionInputs(
                session_id=session_id,
                config=config,
                store=node_store,
                lock_table=lock_table,
                registry=registry,
                log=self._log,
                conflict_detector=conflict_detector,
                deadlock_detector=deadlock_detector,
                git=git_helper,
                test_runner=test_runner,
                max_attempts=max_attempts,
                default_agent_type=default_agent_type,
                agent_pool=agent_pool,
                heartbeat_interval_s=heartbeat_interval_s,
                collect_timeout_s=collect_timeout_s,
                project_lease=project_lease,
                gate_runner=gate_runner,
                adjudicator_llm=adjudicator_llm,
            )
        )
        # The most recent wave's result, kept so teardown can gate a push on
        # something even when the caller has no aggregate to hand it.
        self._last_result: SessionResult | None = None
        # The user objective is durable repair context. Without it, deleting the
        # feature that caused a defect can look structurally clean while undoing
        # the request. Cascade state history prevents a recovered run from
        # forgetting that it has already visited a broken repository state.
        self._objective: str | None = None
        self._cascade_history: list[str] = []
        self._max_concurrent = max(1, config.session.max_concurrent_agents)
        self.state = SessionState.CREATED
        # Everything the current wave accumulates. Replaced whole by
        # ``install_plan`` and ``recover`` — never reset field by field.
        self._wave = WaveState.start()
        self._executor: ThreadPoolExecutor | None = None
        self._concurrent_runner: ConcurrentRunner | None = None

    # -- introspection -----------------------------------------------------

    @property
    def wave(self) -> WaveState:
        """The current wave's state (fresh after every ``install_plan``)."""
        return self._wave

    @property
    def batches(self) -> BatchProcessor:
        """Collects and settles agent results (``collect``, ``process_batch``)."""
        return self._parts.batches

    @property
    def pipeline(self) -> CommitPipeline:
        """The ordered commit checks and the apply step behind them."""
        return self._parts.pipeline

    @property
    def post_wave(self) -> PostWaveAnalyzer:
        """Post-wave analysis and the fix-up tasks it builds."""
        return self._parts.post_wave

    @property
    def last_plan_findings(self) -> list[PlanFinding]:
        """Plan-validation findings of the most recently installed plan."""
        return self._wave.plan_findings

    # -- workers -----------------------------------------------------------

    def _runner(self) -> ConcurrentRunner:
        """Lazily build the thread-pool-backed runner (and its executor)."""
        if self._concurrent_runner is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_concurrent,
                thread_name_prefix=f"mak-{self.session_id}",
            )
            self._concurrent_runner = ConcurrentRunner(
                self._agent_runner,
                self._executor,
                self._parts.batches.completions,
                lambda bundle: self._parts.enricher.enrich(self._wave, bundle),
            )
        return self._concurrent_runner

    def close(self, *, wait: bool = True) -> None:
        """Shut down the worker pool and any agent subprocesses. Repeatable.

        ``wait=False`` is for the abnormal exit: the collect timeout exists so a
        wedged agent cannot stall a run forever, and a shutdown that joined that
        very call would hang the run anyway. Declining to join lets the session
        report what it has.

        ``cancel_futures`` drops work still *queued*; it cannot interrupt a call
        already in flight. What bounds that one is the per-request SDK timeout
        threaded through from ``AgentConfig.timeout`` — the two are a pair.
        Without the timeout the worker thread would outlive the run, and a pool
        worker is non-daemon, so the process would not exit until the provider
        gave up on its own.
        """
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
            self._executor = None
            self._concurrent_runner = None
        # Pooled CLI agent subprocesses outlive the thread pool: the agent runner
        # owns them, and without this call every agent process a run spawned
        # would survive until the interpreter exited. Duck-typed because the
        # injected Assigner protocol does not (and need not) declare it.
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
        # what makes the ``clear()`` below sound: stripping the leases is only
        # safe once this session is known to be the project's sole owner.
        self._acquire_project()
        # A crashed run can leave a commit half-installed. Resolve it before the
        # store is read for anything else, so ingestion never reconciles against
        # a working tree that is mid-transaction.
        self._parts.recovery.recover_journal()
        # A fresh session owns none of the leases a prior (possibly killed) run left
        # in the persisted lock table; drop them so they don't surface later as
        # spurious "lease expired" warnings. Crash recovery uses recover() instead.
        self._lock_table.clear()
        # Read before pruning and reconciling, so both honor it. When the file does
        # not exist yet its defaults apply in memory; it is only written after the
        # clean-tree check below, which a fresh untracked file would otherwise fail.
        self._parts.reconciler.load_ignore_rules()
        pruned = self.prune_excluded_nodes()
        self._parts.reconciler.reconcile()
        self._parts.reconciler.require_clean_tree()
        self._parts.reconciler.write_ignore_file()
        self._parts.post_wave.take_gate_baseline()
        self._parts.reconciler.ensure_audit_repo()
        self.state = SessionState.INITIALIZED
        inventory = self._node_store.list_nodes()
        self._log(
            EventType.SESSION_STARTED,
            node_count=len(inventory),
            pruned_nodes=pruned,
        )
        return inventory

    def prune_excluded_nodes(self) -> int:
        """Drop stored nodes whose file is no longer ingestable; return the count."""
        return self._parts.reconciler.prune_excluded_nodes()

    def _acquire_project(self) -> None:
        """Take the project's exclusive lease, if one was supplied."""
        if self._project_lease is not None:
            self._project_lease.acquire()

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
        proposal = self.propose_plan(user_task)
        validated, findings = proposal.subtasks, proposal.findings
        reviewed = validated
        if review:
            reviewed = display_plan_for_review(
                validated, findings=findings, prompt_fn=prompt_fn, printer=printer
            )
        self.install_plan(reviewed)
        if reviewed is validated:
            # Not edited: keep the richer first-pass findings (install_plan's
            # re-validation of an already-corrected plan reports fewer).
            self._wave.plan_findings = findings
        return reviewed

    def propose_plan(self, user_task: str) -> PlanProposal:
        """Decompose and validate ``user_task``; neither review nor install it.

        The public planning entry point for a front end that reviews the plan
        its own way (the interactive app) before calling :meth:`install_plan`.
        """
        if self.state is not SessionState.INITIALIZED:
            raise SessionError(f"cannot plan from state {self.state}")
        if self._planner is None:
            raise SessionError(
                "no planner is configured for this session; use install_plan() "
                "instead, or set a planner model with /planner in the app"
            )
        self._objective = user_task
        decomposed = self._planner.decompose(
            user_task, self._node_store.list_nodes()
        )
        # Ground and augment the plan before review so the reviewer sees the
        # corrected plan and exactly what validation changed. install_plan()
        # re-validates (an idempotent no-op on the already-corrected plan).
        subtasks, findings = self._parts.planning.validate(decomposed)
        return PlanProposal(subtasks=subtasks, findings=findings)

    def install_plan(
        self, subtasks: list[SubTask], *, objective: str | None = None
    ) -> None:
        """Build the DAG + scheduler from a ready plan (bypasses the planner).

        Also accepted from ``COMPLETED`` and ``FAILED`` so a cascade wave can
        be installed immediately after a finished wave without re-initializing.
        The new wave gets a fresh :class:`WaveState`; nothing the previous wave
        accumulated is visible to it.
        """
        if self.state not in (
            SessionState.INITIALIZED,
            SessionState.PLANNED,
            SessionState.COMPLETED,
            SessionState.FAILED,
        ):
            raise SessionError(f"cannot install a plan from state {self.state}")
        if objective is not None:
            self._objective = objective
        # A new wave starts from nothing: the whole per-wave state is replaced,
        # here, before anything below can fail and leave a half-built wave.
        # The wave's starting inventory is captured before anything runs: a
        # target whose file is not in it cannot be the subject of a credible
        # "nothing needed changing", because there was nothing there to look at.
        self._wave = WaveState.start(
            preexisting_files={
                str(node_id).split("::", 1)[0]
                for node_id in self._node_store.list_nodes()
            }
        )
        self._parts.adjudicator.start_wave()
        # Validation grounds ids and adds missing edges here, so every way a plan
        # arrives — plan(), the app's direct install, cascade waves, user-edited
        # plans — is validated. Idempotent when plan() already validated it.
        self._parts.planning.reject_unsafe_targets(subtasks)
        # Always built: interface locks read callees off it, validation reads
        # references, and post-wave cascade needs the *pre*-wave edges to find
        # callers of symbols the wave deletes.
        self._wave.graph = dep_graph_from_store(self._node_store)
        subtasks, findings = self._parts.planning.validate(subtasks, self._wave.graph)
        self._wave.plan_findings = findings
        if findings:
            self._parts.planning.log_findings(findings)
        subtasks = self._parts.planning.assign_agents(subtasks)
        self._wave.lock_policy = build_lock_policy(
            self._config.semantic, self._node_store, self._wave.graph, subtasks
        )
        self._wave.scheduler = self._build_scheduler(subtasks)
        self._wave.progress = {
            t.task_id: SubTaskProgress(t.task_id, list(t.target_nodes))
            for t in subtasks
        }
        self.state = SessionState.PLANNED

    def _build_scheduler(self, subtasks: list[SubTask]) -> Scheduler:
        """Build and persist the wave's scheduler, with the session annotations."""
        soft: dict[str, set[str]] = {}
        if self._config.semantic.contract_dispatch:
            subtasks, soft = soft_edges(subtasks, self._wave.lock_policy)
        scheduler = Scheduler(
            DAG(subtasks, soft_edges=soft),
            self._lock_table,
            self._runner(),
            self._registry,
            persist_path=self._parts.workspace.task_graph_path,
            max_concurrent=self._max_concurrent,
            lock_policy=self._wave.lock_policy,
        )
        if self._objective is not None:
            scheduler.annotations["objective"] = self._objective
        if self._cascade_history:
            scheduler.annotations["cascade_history"] = list(self._cascade_history)
        scheduler.save()
        return scheduler

    # -- phase 3: run ------------------------------------------------------

    def run(self, max_iterations: int = 1000) -> SessionResult:
        """Drive the concurrent scheduler loop until done or progress stalls."""
        if self.state is not SessionState.PLANNED:
            raise SessionError(f"cannot run from state {self.state}")
        scheduler = self._wave.require_scheduler()
        self.state = SessionState.RUNNING

        watchdog = self._parts.watchdog
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=watchdog.run_heartbeat,
            args=(stop, self._wave),
            name=f"mak-heartbeat-{self.session_id}",
            daemon=True,
        )
        heartbeat.start()
        self._wave.wedged = False
        try:
            self._run_loop(scheduler, max_iterations)
        finally:
            stop.set()
            heartbeat.join(timeout=watchdog.heartbeat_interval_s + 1.0)
            # A wedged worker must not be waited on — that is the hang the
            # collect timeout exists to prevent.
            self.close(wait=not self._wave.wedged)

        result = wave_result(self._wave, scheduler, self._log)
        self.state = result.state
        # Retained so ``teardown`` has something to gate on when its caller does
        # not supply an aggregate: the next ``install_plan`` replaces the wave
        # this was built from, so the result is all that survives it.
        self._last_result = result
        return result

    def _run_loop(self, scheduler: Scheduler, max_iterations: int) -> None:
        """Dispatch concurrently, collect batches, and process them to completion."""
        wave, parts = self._wave, self._parts
        deadlock_interval = self._config.session.deadlock_check_interval_s
        last_deadlock_scan = time.monotonic()
        for _iteration in range(max_iterations):
            if self._budget_breach() is not None:
                self._stop_on_budget(scheduler)
                break
            scheduler.tick()
            parts.retry.submit_partials(wave, self._runner)
            # Sample realized parallelism: how many tasks are in flight this tick.
            wave.concurrency_samples.append(len(scheduler.dispatched))

            now = time.monotonic()
            if now - last_deadlock_scan >= deadlock_interval:
                parts.watchdog.check_deadlocks(wave)
                last_deadlock_scan = now

            if scheduler.is_done():
                break
            if not scheduler.dispatched:
                # Nothing is in flight and the DAG is not done — the remaining
                # tasks are blocked on locks that never freed, or stranded.
                break
            if parts.parking.all_in_flight_parked(wave, scheduler):
                parts.batches.release_parked_victim(wave)
                continue

            batch = parts.batches.collect()
            if not batch:
                # Collection timed out with work in flight: a worker is wedged.
                # Recorded so ``close`` does not then block joining it.
                wave.wedged = True
                self._log(
                    EventType.SESSION_ENDED,
                    wedged=True,
                    in_flight=sorted(scheduler.dispatched),
                    collect_timeout_s=parts.batches.collect_timeout_s,
                )
                break
            parts.batches.process_batch(wave, batch)

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
        self._wave.budget_stop = self._budget_breach()
        self._log(
            EventType.SESSION_ENDED,
            budget_exhausted=True,
            max_total_tokens=self._config.session.max_total_tokens,
            total_tokens=self.total_tokens,
            in_flight=sorted(scheduler.dispatched),
        )
        self._parts.batches.drain(self._wave, scheduler)

    @property
    def token_usage(self) -> dict[str, int]:
        """Tokens spent this session: every agent call plus the planner's.

        Sourced from what each provider reported on its own response, so a
        streamed call counts exactly like a non-streamed one.
        """
        return combined_usage(self._parts.batches.agent_usage, self._planner)

    @property
    def total_tokens(self) -> int:
        """Input + output tokens across agents and planner."""
        return total_tokens(self.token_usage)

    # -- phase 4: teardown -------------------------------------------------

    def teardown(self, execution: ExecutionResult | None = None) -> TeardownResult:
        """Run the test suite and decide, honestly, whether to push.

        ``execution`` is the aggregate over every wave when the caller has one
        (it knows about cascade waves; the session does not); without it the
        push is gated on this session's last result. See
        :meth:`mak.session.finalize.Finalizer.teardown`.
        """
        return self._parts.finalizer.teardown(
            self._wave, execution, self._last_result
        )

    # -- crash recovery ----------------------------------------------------

    def recover(self) -> int:
        """Expire stale leases and re-queue incomplete tasks from disk.

        Returns the number of leases expired. Must be called before ``run`` when
        resuming a crashed session; rebuilds the scheduler from ``task_graph.json``
        if one is present. A task graph that cannot be read leaves the session
        un-planned rather than raising, so the caller reports "nothing to
        recover" and the operator can start a fresh run.
        """
        # Same ordering rule as ``initialize``: ownership, then any commit the
        # crash left in flight, and only then the state that depends on both.
        self._acquire_project()
        self._parts.recovery.recover_journal()
        expired, restored = self._parts.recovery.restore(self._runner)
        if restored is not None:
            self._wave = restored.wave
            self._objective = restored.objective
            self._cascade_history = restored.cascade_history
            self.state = SessionState.PLANNED
        return expired

    # -- cascade -----------------------------------------------------------

    def detect_cascade_tasks(self) -> list[SubTask]:
        """Return fix-up tasks for everything this wave left broken between tasks.

        See :meth:`mak.session.post_wave.PostWaveAnalyzer.detect_cascade_tasks`.
        """
        return self._parts.post_wave.detect_cascade_tasks(self._wave, self._objective)

    def detect_cross_module_defects(self) -> list[CrossModuleDefect]:
        """Report where the files this wave wrote contradict the rest of the code."""
        return self._parts.post_wave.detect_cross_module_defects(self._wave)

    def cascade_state_fingerprint(self, tasks: list[SubTask]) -> str:
        """Digest the broken state and repair scope shown for cascade review."""
        return self._parts.post_wave.fingerprint(tasks)

    def cascade_history(self) -> tuple[str, ...]:
        """Return repository states already presented during this session."""
        return tuple(self._cascade_history)

    def remember_cascade_state(self, fingerprint: str) -> None:
        """Persist one presented cascade state alongside the active task graph."""
        if fingerprint not in self._cascade_history:
            self._cascade_history.append(fingerprint)
        scheduler = self._wave.scheduler
        if scheduler is not None:
            scheduler.annotations["cascade_history"] = list(self._cascade_history)
            scheduler.save()
