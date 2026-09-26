"""Parked commits: finished work waiting for a lock or a contract provider."""

from __future__ import annotations

from collections.abc import Callable

from mak.core.logging import EventType
from mak.core.types import NodeFragment, NodeId, TaskBundle, TaskResult
from mak.scheduler.scheduler import Scheduler
from mak.session.events import EventLog
from mak.session.store_view import StoreView
from mak.session.wave import Parked, WaveState

# Re-settles a parked result: (bundle, result, staged ids, batch peers).
Settle = Callable[[TaskBundle, TaskResult, list[NodeId], dict[str, str]], object]


class ParkedCommits:
    """Park a result whose commit must wait, and resume it when it can proceed.

    The agent's work is fine; another in-flight task holds something the
    commit needs. Re-running the agent would spend a whole call — and an
    attempt of the retry budget — to arrive at the same result, so the result
    is parked and its commit retried when locks are released.
    """

    def __init__(self, *, view: StoreView, log: EventLog) -> None:
        self._view = view
        self._log = log

    @staticmethod
    def defer(
        wave: WaveState, task_id: str, reason: str, *, providers: bool = False
    ) -> None:
        """Mark the commit in progress as waiting (on a lock, or on providers)."""
        wave.deferring[task_id] = reason
        if providers:
            wave.waiting_on_providers.add(task_id)
        else:
            wave.waiting_on_providers.discard(task_id)

    def park(
        self,
        wave: WaveState,
        bundle: TaskBundle,
        result: TaskResult,
        staged: list[NodeId],
    ) -> None:
        """Take the attempt's staged sources out of the store and park them."""
        task_id = bundle.task_id
        sources: dict[NodeId, str] = {}
        for node_id in staged:
            fragment = self._view.store.get_staged(node_id)
            if fragment is not None:
                sources[node_id] = fragment.source
            self._view.store.rollback_node(node_id)
        reason = wave.deferring.pop(task_id, "waiting for a lock")
        wave.parked[task_id] = Parked(
            bundle, result, sources, reason,
            on_providers=task_id in wave.waiting_on_providers,
        )
        self._log(EventType.COMMIT_DEFERRED, task_id=task_id, reason=reason)

    def resume(self, wave: WaveState, settle: Settle) -> None:
        """Retry every parked commit until a pass makes no progress."""
        progressed = True
        while progressed and wave.parked:
            progressed = False
            for task_id in sorted(wave.parked):
                if wave.parked[task_id].on_providers and _providers_pending(
                    wave, task_id
                ):
                    continue
                parked = wave.parked.pop(task_id)
                for node_id, source in parked.sources.items():
                    self._view.store.put_node(
                        node_id,
                        NodeFragment(node_id, self._view.kind(node_id), source, 1),
                    )
                self._log(EventType.COMMIT_DEFERRED, task_id=task_id, resumed=True)
                settle(parked.bundle, parked.result, list(parked.sources), {})
                if task_id not in wave.parked:
                    progressed = True

    @staticmethod
    def all_in_flight_parked(wave: WaveState, scheduler: Scheduler) -> bool:
        """Whether every in-flight task is a parked result (none can progress)."""
        return (
            bool(wave.parked)
            and not wave.partial_queue
            and scheduler.dispatched <= set(wave.parked)
        )

    def release_victim(
        self, wave: WaveState, scheduler: Scheduler, settle: Settle
    ) -> None:
        """Break a cycle of parked results by re-queueing one of them.

        Parking is the one place a task waits while holding locks, so it is the
        one place a wait can become a cycle: W waits for R's interface read
        lock while R waits to take a table W appends to. The last task by id
        gives its locks back and is re-queued with fresh context; the rest are
        retried at once. A task waiting on its *contract providers* is instead
        re-gated on them, since re-dispatching it at once would only park it
        again.
        """
        victim = sorted(wave.parked)[-1]
        parked = wave.parked.pop(victim)
        progress = wave.progress[victim]
        if parked.on_providers:
            wave.granted.pop(victim, None)
            self._log(
                EventType.COMMIT_DEFERRED, task_id=victim, released=True,
                reason="re-gated on its contract providers",
            )
            scheduler.wait_for_dependencies(victim)
            self.resume(wave, settle)
            return
        progress.kernel_note = (
            f"Your previous result could not be committed ({parked.reason}), and "
            "the tasks holding what it needed were waiting on this one in turn. "
            "It was released so they could finish. Redo the task against the "
            "current code in your refreshed bundle."
        )
        progress.error_kind = "commit_cycle"
        wave.granted.pop(victim, None)
        wave.redispatches += 1
        self._log(EventType.COMMIT_DEFERRED, task_id=victim, released=True)
        scheduler.on_task_failed(victim, requeue=True)
        self.resume(wave, settle)


def _providers_pending(wave: WaveState, task_id: str) -> bool:
    """Whether a soft provider of ``task_id`` has neither committed nor failed."""
    dag = wave.require_scheduler().dag
    return any(
        not dag.is_complete(dep) and dep not in wave.failed
        for dep in dag.soft_dependencies(task_id)
    )
