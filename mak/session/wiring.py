"""Build a session's collaborators, once, from what the session was given."""

from __future__ import annotations

from dataclasses import dataclass

from mak.agent_runner.registry import AdapterRegistry
from mak.config import MakConfig
from mak.conflict_detector.detector import ConflictDetector
from mak.git_integration.git import GitHelper
from mak.lock_manager.deadlock_detector import DeadlockDetector
from mak.lock_manager.project_lease import ProjectLease
from mak.node_store.store import NodeStore
from mak.planner.planner import PlannerLLM
from mak.semantic.gate_types import ProcessRunner, run_process
from mak.semantic.gates import GateSuite
from mak.session.adjudication import AdjudicatorFence
from mak.session.batch import BatchProcessor
from mak.session.commit.apply import CommitApplier
from mak.session.commit.pipeline import CommitPipeline, default_checks
from mak.session.dispatch import DispatchEnricher
from mak.session.events import EventLog
from mak.session.finalize import Finalizer
from mak.session.fixups import FixupBuilder
from mak.session.outcomes import NoopPolicy, RetryPolicy
from mak.session.parking import ParkedCommits
from mak.session.planning import PlanPreparer
from mak.session.post_wave import PostWaveAnalyzer
from mak.session.reconcile import WorkTreeReconciler
from mak.session.recovery import RecoveryManager
from mak.session.store_view import StoreView
from mak.session.types import LockTableLike, TestRunner
from mak.session.watchdog import LockWatchdog
from mak.session.workspace import Workspace


@dataclass(frozen=True)
class SessionInputs:
    """Everything a session was constructed with that its collaborators need."""

    session_id: str
    config: MakConfig
    store: NodeStore
    lock_table: LockTableLike
    registry: AdapterRegistry
    log: EventLog
    conflict_detector: ConflictDetector | None
    deadlock_detector: DeadlockDetector | None
    git: GitHelper | None
    test_runner: TestRunner | None
    max_attempts: int
    default_agent_type: str | None
    agent_pool: list[str] | None
    heartbeat_interval_s: float | None
    collect_timeout_s: float
    project_lease: ProjectLease | None
    gate_runner: ProcessRunner | None
    adjudicator_llm: PlannerLLM | None


@dataclass(frozen=True)
class Collaborators:
    """The single-purpose objects a session delegates to; one set per session."""

    view: StoreView
    workspace: Workspace
    reconciler: WorkTreeReconciler
    planning: PlanPreparer
    enricher: DispatchEnricher
    adjudicator: AdjudicatorFence
    pipeline: CommitPipeline
    parking: ParkedCommits
    retry: RetryPolicy
    batches: BatchProcessor
    recovery: RecoveryManager
    finalizer: Finalizer
    watchdog: LockWatchdog
    post_wave: PostWaveAnalyzer


def wire(inputs: SessionInputs) -> Collaborators:
    """Build every collaborator for one session."""
    config, log = inputs.config, inputs.log
    view = StoreView(inputs.store)
    workspace = Workspace(config.session)
    adjudicator = AdjudicatorFence(
        config=config.semantic, llm=inputs.adjudicator_llm, log=log
    )
    parking = ParkedCommits(view=view, log=log)
    pipeline, retry, batches = _commit_path(
        inputs, view, workspace, adjudicator, parking
    )
    return Collaborators(
        view=view,
        workspace=workspace,
        reconciler=WorkTreeReconciler(
            config=config,
            store=inputs.store,
            workspace=workspace,
            git=inputs.git,
            log=log,
        ),
        planning=PlanPreparer(
            config=config,
            store=inputs.store,
            workspace=workspace,
            registry=inputs.registry,
            agent_pool=inputs.agent_pool,
            default_agent_type=inputs.default_agent_type,
            log=log,
        ),
        enricher=DispatchEnricher(view=view, config=config.session, log=log),
        adjudicator=adjudicator,
        pipeline=pipeline,
        parking=parking,
        retry=retry,
        batches=batches,
        recovery=_recovery(inputs, workspace),
        finalizer=Finalizer(
            config=config, git=inputs.git, test_runner=inputs.test_runner, log=log
        ),
        watchdog=_watchdog(inputs),
        post_wave=PostWaveAnalyzer(
            view=view,
            workspace=workspace,
            # The runner is injectable so tests need none of the gates' tools.
            gates=GateSuite(config.semantic, runner=inputs.gate_runner or run_process),
            config=config.semantic,
            fixups=FixupBuilder(
                view=view, default_agent_type=inputs.default_agent_type
            ),
            log=log,
        ),
    )


def _commit_path(
    inputs: SessionInputs,
    view: StoreView,
    workspace: Workspace,
    adjudicator: AdjudicatorFence,
    parking: ParkedCommits,
) -> tuple[CommitPipeline, RetryPolicy, BatchProcessor]:
    """Build the pipeline and the result processing that feeds it."""
    config, log = inputs.config, inputs.log
    applier = CommitApplier(
        config=config,
        view=view,
        workspace=workspace,
        git=inputs.git,
        session_id=inputs.session_id,
        log=log,
    )
    pipeline = CommitPipeline(
        checks=default_checks(
            semantic=config.semantic,
            detector=inputs.conflict_detector or ConflictDetector(),
            # The only model call on the commit path, and only when configured.
            adjudicator=adjudicator if adjudicator.configured else None,
        ),
        applier=applier,
        view=view,
        lock_table=inputs.lock_table,
        parking=parking,
        max_attempts=inputs.max_attempts,
        log=log,
    )
    retry = RetryPolicy(
        registry=inputs.registry, max_attempts=inputs.max_attempts, log=log
    )
    batches = BatchProcessor(
        view=view,
        lock_table=inputs.lock_table,
        pipeline=pipeline,
        parking=parking,
        noop=NoopPolicy(
            view=view,
            workspace=workspace,
            lock_table=inputs.lock_table,
            applier=applier,
            log=log,
        ),
        retry=retry,
        collect_timeout_s=inputs.collect_timeout_s,
        log=log,
    )
    return pipeline, retry, batches


def _recovery(inputs: SessionInputs, workspace: Workspace) -> RecoveryManager:
    return RecoveryManager(
        config=inputs.config,
        store=inputs.store,
        workspace=workspace,
        lock_table=inputs.lock_table,
        registry=inputs.registry,
        git=inputs.git,
        session_id=inputs.session_id,
        max_concurrent=max(1, inputs.config.session.max_concurrent_agents),
        log=inputs.log,
    )


def _watchdog(inputs: SessionInputs) -> LockWatchdog:
    session_cfg = inputs.config.session
    return LockWatchdog(
        lock_table=inputs.lock_table,
        project_lease=inputs.project_lease,
        detector=inputs.deadlock_detector or DeadlockDetector(),
        heartbeat_interval_s=(
            inputs.heartbeat_interval_s
            if inputs.heartbeat_interval_s is not None
            else max(1.0, session_cfg.lock_timeout_s / 3.0)
        ),
        log=inputs.log,
    )
