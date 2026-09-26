"""Turn a finished wave into its ``SessionResult`` and plan-quality metrics."""

from __future__ import annotations

from mak.core.logging import EventType
from mak.core.types import SubTask
from mak.scheduler.scheduler import Scheduler
from mak.session.events import EventLog
from mak.session.types import SessionResult, SessionState
from mak.session.wave import WaveState


def wave_result(wave: WaveState, scheduler: Scheduler, log: EventLog) -> SessionResult:
    """Compute the terminal state and result after the run loop exits.

    A task that is neither completed nor explicitly failed was stranded. It
    must NOT be reported as success — the run is COMPLETED only when the DAG is
    genuinely done. The strays are split: those with a failed ancestor are
    *skipped* (an expected downstream consequence), the rest are *blocked*.
    """
    accounted = set(wave.completed) | set(wave.failed)
    unaccounted = [tid for tid in scheduler.dag.remaining() if tid not in accounted]
    tainted = failed_descendants(wave, scheduler.dag.tasks)
    skipped = [tid for tid in unaccounted if tid in tainted]
    blocked = [tid for tid in unaccounted if tid not in tainted]
    completed = (
        scheduler.is_done()
        and not wave.failed
        and not blocked
        and not skipped
        and wave.budget_stop is None
    )
    if skipped or blocked:
        log(
            EventType.SESSION_ENDED,
            skipped=skipped,
            blocked=blocked,
            stalled=True,
        )
    metrics = plan_metrics(wave)
    log(EventType.PLAN_METRICS, **metrics)
    return SessionResult(
        state=SessionState.COMPLETED if completed else SessionState.FAILED,
        completed=tuple(wave.completed),
        failed=tuple(wave.failed),
        blocked=tuple(blocked),
        skipped=tuple(skipped),
        noop=tuple(noop_task_ids(wave)),
        failure_reasons={
            t: wave.failure_reasons[t] for t in wave.failed if t in wave.failure_reasons
        },
        metrics=metrics,
        stopped_reason=wave.budget_stop,
    )


def plan_metrics(wave: WaveState) -> dict[str, float]:
    """Realized-parallelism and rework metrics for the wave just run.

    ``tasks_completed`` counts every task that closed — but a task closes only
    by producing work or by *asserting* there was none, and ``tasks_noop``
    says how many did the latter, so the headline number does not overstate
    what a run did.

    ``context_bytes_total`` / ``mean_context_bytes`` are the input side of the
    same accounting: what the run actually *gave* its agents. A wave whose
    mean is near zero produced its results without being shown the code, which
    is worth knowing before trusting them — and ``starved_dispatches`` counts
    the ones the kernel refused outright.

    ``adjudicated_accepts`` counts the stale reads the LLM adjudicator accepted:
    the only commit decisions in a wave that a model, not the kernel, made.
    """
    samples = wave.concurrency_samples
    mean = round(sum(samples) / len(samples), 2) if samples else 0.0
    mean_bytes = (
        round(wave.context_bytes / wave.dispatches, 2) if wave.dispatches else 0.0
    )
    return {
        "max_concurrency": float(max(samples, default=0)),
        "mean_concurrency": mean,
        "conflict_rejections": float(wave.conflict_rejections),
        "redispatches": float(wave.redispatches),
        "tasks_completed": float(len(wave.completed)),
        "tasks_noop": float(len(noop_task_ids(wave))),
        "tasks_failed": float(len(wave.failed)),
        "dispatches": float(wave.dispatches),
        "context_bytes_total": float(wave.context_bytes),
        "mean_context_bytes": mean_bytes,
        "starved_dispatches": float(wave.starved_dispatches),
        "stale_reads": float(wave.stale_reads),
        "stale_redispatches": float(wave.stale_redispatches),
        "adjudicated_accepts": float(wave.adjudicated_accepts),
    }


def noop_task_ids(wave: WaveState) -> list[str]:
    """Completed tasks where *every* closed grant was an asserted no-op.

    Derived rather than tracked, so a task that changed one node and declined
    another still counts as work done — the distinction only matters when a
    task produced nothing at all.
    """
    noop: list[str] = []
    for task_id in wave.completed:
        progress = wave.progress.get(task_id)
        if (
            progress is not None
            and progress.noop_nodes
            and progress.completed_nodes <= progress.noop_nodes
        ):
            noop.append(task_id)
    return noop


def failed_descendants(wave: WaveState, tasks: dict[str, SubTask]) -> set[str]:
    """Tasks that (transitively) depend on a failed task.

    Iterates to a fixpoint over the dependency edges so a failure propagates the
    whole way down the chain (a task depending on a skipped task is skipped too).
    """
    tainted = set(wave.failed)
    changed = True
    while changed:
        changed = False
        for tid, task in tasks.items():
            if tid in tainted:
                continue
            if any(dep in tainted for dep in task.depends_on):
                tainted.add(tid)
                changed = True
    return tainted - set(wave.failed)
