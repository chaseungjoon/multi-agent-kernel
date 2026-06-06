"""Scheduler: drives DAG traversal, lock pre-allocation, and agent dispatch.

The scheduler converts a ``DAG`` of ``SubTask`` nodes into running work (PLANS.md
§9). Its safety property is **atomic lock pre-allocation** (§9.2): before a task
is dispatched, *all* of its write locks are acquired in one ``try_acquire_all``
call. If any lock is unavailable, the task stays in the ready queue and is retried
on the next ``tick`` — partial acquisition (the classic deadlock setup) never
happens.

The scheduler depends only on injected collaborators (a lock manager, an adapter
registry, and an agent runner), described by the ``Protocol`` classes below, so it
can be unit-tested with mocks and is decoupled from the concrete Wave-2 lock and
runner implementations.

DAG execution state is persisted to ``.mak/task_graph.json`` after every state
transition so a crashed session can be recovered (Wave 3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from mak.core.types import LockMode, NodeId, SubTask
from mak.scheduler.dag import DAG


class LockManager(Protocol):
    """The subset of ``LockTable`` the scheduler relies on."""

    def try_acquire_all(
        self, requests: list[tuple[NodeId, LockMode]], holder: str
    ) -> bool:
        """Atomically acquire every requested lock, or none."""
        ...

    def release_all(self, holder: str) -> int:
        """Release every lock held by ``holder``."""
        ...


class AgentAdapterLike(Protocol):
    """Marker protocol for an adapter resolved from the registry."""

    agent_type: str


class AdapterRegistryLike(Protocol):
    """The subset of ``AdapterRegistry`` the scheduler relies on."""

    def get(self, agent_type: str) -> AgentAdapterLike:
        """Resolve and instantiate an adapter for ``agent_type``."""
        ...


class AgentRunnerLike(Protocol):
    """The subset of ``AgentRunner`` the scheduler relies on."""

    def assign(self, adapter: AgentAdapterLike, task: object) -> object:
        """Dispatch a task bundle to an adapter and return its result."""
        ...


class Scheduler:
    """Dispatches DAG-ordered tasks to agents under atomic lock pre-allocation."""

    def __init__(
        self,
        dag: DAG,
        lock_manager: LockManager,
        agent_runner: AgentRunnerLike,
        registry: AdapterRegistryLike,
        persist_path: Path | None = None,
    ) -> None:
        self._dag = dag
        self._lock_manager = lock_manager
        self._agent_runner = agent_runner
        self._registry = registry
        self._persist_path = persist_path

        self._dispatched: set[str] = set()
        self.ready_queue: list[SubTask] = list(dag.newly_unblocked())
        self._save()

    @property
    def dispatched(self) -> set[str]:
        """Return ids of tasks currently dispatched (in flight)."""
        return set(self._dispatched)

    def _lock_requests(self, task: SubTask) -> list[tuple[NodeId, LockMode]]:
        """Build a write-lock request for each of the task's target nodes."""
        return [(node_id, LockMode.WRITE) for node_id in task.target_nodes]

    def tick(self) -> list[str]:
        """Dispatch every ready task whose locks can be acquired now.

        Returns the ids of tasks dispatched during this tick. Tasks whose locks
        are unavailable are left in the ready queue for a later tick.
        """
        dispatched_now: list[str] = []
        for task in list(self.ready_queue):
            if self._lock_manager.try_acquire_all(
                self._lock_requests(task), holder=task.task_id
            ):
                self.ready_queue.remove(task)
                self.dispatch(task)
                dispatched_now.append(task.task_id)
        if dispatched_now:
            self._save()
        return dispatched_now

    def dispatch(self, task: SubTask) -> None:
        """Resolve an adapter for the task and hand it to the agent runner."""
        adapter = self._registry.get(task.agent_type)
        bundle = self._to_bundle(task)
        self._dispatched.add(task.task_id)
        self._agent_runner.assign(adapter, bundle)

    @staticmethod
    def _to_bundle(task: SubTask) -> object:
        """Build the ``TaskBundle`` wire object the runner expects from a SubTask."""
        # Imported lazily to keep the scheduler's hard dependency surface to core
        # types; TaskBundle is the agent-protocol unit, not a scheduler concept.
        from mak.core.types import TaskBundle

        return TaskBundle(
            task_id=task.task_id,
            description=task.description,
            target_nodes=list(task.target_nodes),
        )

    def on_task_complete(self, task_id: str) -> None:
        """Release the task's locks, mark it done, and enqueue newly unblocked work."""
        self._lock_manager.release_all(task_id)
        self._dispatched.discard(task_id)
        self._dag.mark_complete(task_id)
        self.ready_queue.extend(self._dag.newly_unblocked())
        self._save()

    def on_task_failed(self, task_id: str, requeue: bool = True) -> None:
        """Release the task's locks and optionally re-queue it for another attempt."""
        self._lock_manager.release_all(task_id)
        self._dispatched.discard(task_id)
        if requeue and not self._dag.is_complete(task_id):
            task = self._dag.get_task(task_id)
            if task not in self.ready_queue:
                self.ready_queue.append(task)
        self._save()

    def is_done(self) -> bool:
        """Return whether all tasks are complete and nothing is ready or in flight."""
        return (
            self._dag.all_complete()
            and not self.ready_queue
            and not self._dispatched
        )

    def run(self, max_ticks: int | None = None) -> None:
        """Tick until the DAG is complete (drives a synchronous, blocking runner).

        Assumes ``agent_runner.assign`` is synchronous and the caller invokes
        ``on_task_complete`` as part of dispatch, or that a subclass overrides the
        loop for an async runner. ``max_ticks`` bounds the loop for safety.
        """
        ticks = 0
        while not self.is_done():
            if max_ticks is not None and ticks >= max_ticks:
                break
            before = len(self._dispatched)
            self.tick()
            ticks += 1
            # No progress and nothing in flight → blocked (locks unavailable
            # forever, or a task never completed). Avoid spinning.
            if not self.ready_queue and len(self._dispatched) == before:
                break

    # -- persistence -------------------------------------------------------

    def _state(self) -> dict[str, object]:
        return {
            "tasks": [
                {
                    "task_id": t.task_id,
                    "description": t.description,
                    "target_nodes": list(t.target_nodes),
                    "depends_on": list(t.depends_on),
                    "agent_type": t.agent_type,
                }
                for t in self._dag.tasks.values()
            ],
            "completed": [
                tid for tid in self._dag.topological_order()
                if self._dag.is_complete(tid)
            ],
            "dispatched": sorted(self._dispatched),
            "ready": [t.task_id for t in self.ready_queue],
        }

    def _save(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_path.write_text(
            json.dumps(self._state(), indent=2), encoding="utf-8"
        )

    def save(self) -> None:
        """Persist DAG execution state to ``.mak/task_graph.json``."""
        self._save()

    @classmethod
    def from_persisted(
        cls,
        persist_path: Path,
        lock_manager: LockManager,
        agent_runner: AgentRunnerLike,
        registry: AdapterRegistryLike,
    ) -> Scheduler:
        """Reconstruct a scheduler from a persisted ``task_graph.json``.

        Completed tasks are replayed, and previously dispatched/queued tasks are
        restored without being re-emitted as freshly unblocked. Tasks that were
        in flight at crash time are re-queued for another attempt.
        """
        data = json.loads(persist_path.read_text("utf-8"))
        tasks = [
            SubTask(
                task_id=str(t["task_id"]),
                description=str(t["description"]),
                target_nodes=[NodeId(n) for n in t.get("target_nodes", [])],
                depends_on=[str(d) for d in t.get("depends_on", [])],
                agent_type=str(t.get("agent_type", "")),
            )
            for t in data.get("tasks", [])
        ]
        dag = DAG(tasks)

        completed = [str(c) for c in data.get("completed", [])]
        ready_ids = [str(r) for r in data.get("ready", [])]
        dispatched_ids = [str(d) for d in data.get("dispatched", [])]

        for tid in completed:
            dag.mark_complete(tid)
        # Everything already handed out must not be re-emitted by newly_unblocked.
        for tid in (*completed, *ready_ids, *dispatched_ids):
            dag.mark_released(tid)

        scheduler = cls.__new__(cls)
        scheduler._dag = dag
        scheduler._lock_manager = lock_manager
        scheduler._agent_runner = agent_runner
        scheduler._registry = registry
        scheduler._persist_path = persist_path
        scheduler._dispatched = set()
        # In-flight tasks at crash time are re-queued (locks were lost on crash).
        # Preserve order and avoid duplicates between the persisted ready set and
        # the re-queued in-flight set.
        queued_ids: list[str] = []
        for tid in (*ready_ids, *dispatched_ids):
            if tid not in queued_ids and not dag.is_complete(tid):
                queued_ids.append(tid)
        scheduler.ready_queue = [dag.get_task(tid) for tid in queued_ids]
        # Any tasks that became unblocked but were never queued are picked up too.
        scheduler.ready_queue.extend(dag.newly_unblocked())
        return scheduler
