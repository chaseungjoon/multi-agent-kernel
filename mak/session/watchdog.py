"""Keep in-flight leases alive and scan the lock wait graph for deadlocks."""

from __future__ import annotations

import threading

from mak.core.logging import EventType
from mak.core.types import LockMode, NodeId
from mak.lock_manager.deadlock_detector import DeadlockDetector
from mak.lock_manager.project_lease import ProjectLease
from mak.scheduler.lock_policy import lock_requests
from mak.session.events import EventLog
from mak.session.types import LockTableLike
from mak.session.wave import WaveState


class LockWatchdog:
    """Lease heartbeat for a running wave, plus a defensive deadlock scan."""

    def __init__(
        self,
        *,
        lock_table: LockTableLike,
        project_lease: ProjectLease | None,
        detector: DeadlockDetector,
        heartbeat_interval_s: float,
        log: EventLog,
    ) -> None:
        self._lock_table = lock_table
        self._project_lease = project_lease
        self._detector = detector
        self.heartbeat_interval_s = heartbeat_interval_s
        self._log = log

    def run_heartbeat(self, stop: threading.Event, wave: WaveState) -> None:
        """Renew in-flight tasks' leases until ``stop`` is set.

        A long agent call must not let its lease lapse and get its lock stolen.
        While the run loop is active, every in-flight holder's leases are renewed
        each interval so a slow-but-alive agent keeps its grants.
        """
        while not stop.wait(self.heartbeat_interval_s):
            # The project lease is renewed on the same tick as the task leases:
            # both answer "is this session still alive?", and a run that renews
            # one but not the other can be reported as abandoned mid-wave.
            if self._project_lease is not None:
                self._project_lease.heartbeat()
            scheduler = wave.scheduler
            if scheduler is None:
                continue
            for task_id in scheduler.dispatched:
                self._lock_table.renew_all(task_id)

    def check_deadlocks(self, wave: WaveState) -> None:
        """Scan the wait graph for cycles and resolve any via wound-wait.

        With atomic lock pre-allocation a waiting task holds *no* locks, so the
        wait graph can never contain a cycle — this watchdog is defense in depth.
        Should a cycle ever arise, the youngest task in it is aborted and
        re-queued.
        """
        scheduler = wave.scheduler
        if scheduler is None:
            return
        waiting = [
            (task.task_id, node_id, mode)
            for task in scheduler.ready_queue
            for node_id, mode in lock_requests(task, wave.lock_policy)
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
        graph = self._detector.build_wait_graph(held, waiting)
        for cycle in self._detector.find_cycles(graph):
            victim = self._detector.resolve(cycle, start_times)
            scheduler.on_task_failed(victim, requeue=True)
            self._log(
                EventType.CONFLICT_DETECTED, deadlock=list(cycle), aborted=victim
            )
