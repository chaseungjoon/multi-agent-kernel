"""DAG: dependency graph over the planner's ``SubTask`` list.

The DAG is built from ``SubTask.depends_on`` edges (PLANS.md §9.1). It validates
at construction time — unique task ids, every dependency references a known
task, and the graph is acyclic — raising ``SchedulingError`` otherwise, so a
malformed plan is rejected before any locks or agents are touched.

Execution state is tracked incrementally: ``mark_complete`` records a finished
task and ``newly_unblocked`` returns the tasks that *just* became runnable (all
dependencies complete) and have not been handed out before. Calling
``newly_unblocked`` immediately after construction yields the initial ready set
(tasks with no dependencies), so the scheduler has a single, uniform entry point
for populating its ready queue.
"""

from __future__ import annotations

from mak.core.exceptions import SchedulingError
from mak.core.types import SubTask


class DAG:
    """A validated, stateful dependency graph of ``SubTask`` nodes."""

    def __init__(self, tasks: list[SubTask]) -> None:
        self._tasks: dict[str, SubTask] = {}
        for task in tasks:
            if task.task_id in self._tasks:
                raise SchedulingError(f"duplicate task id: {task.task_id}")
            self._tasks[task.task_id] = task

        self._validate_dependencies()
        self._order: list[str] = self._topological_sort()

        self._complete: set[str] = set()
        self._released: set[str] = set()

    @property
    def tasks(self) -> dict[str, SubTask]:
        """Return a copy of the task map keyed by task id."""
        return dict(self._tasks)

    def get_task(self, task_id: str) -> SubTask:
        """Return the ``SubTask`` for ``task_id`` or raise ``SchedulingError``."""
        if task_id not in self._tasks:
            raise SchedulingError(f"unknown task id: {task_id}")
        return self._tasks[task_id]

    def _validate_dependencies(self) -> None:
        for task in self._tasks.values():
            for dep in task.depends_on:
                if dep not in self._tasks:
                    raise SchedulingError(
                        f"task '{task.task_id}' depends on unknown task '{dep}'"
                    )
                if dep == task.task_id:
                    raise SchedulingError(
                        f"task '{task.task_id}' depends on itself"
                    )

    def _topological_sort(self) -> list[str]:
        """Kahn's algorithm. Raises ``SchedulingError`` if the graph has a cycle."""
        indegree: dict[str, int] = {tid: 0 for tid in self._tasks}
        dependents: dict[str, list[str]] = {tid: [] for tid in self._tasks}
        for tid, task in self._tasks.items():
            # Deduplicate edges so a repeated dependency doesn't skew indegree.
            for dep in dict.fromkeys(task.depends_on):
                indegree[tid] += 1
                dependents[dep].append(tid)

        # Process in stable id order so the result is deterministic.
        ready = sorted(tid for tid, deg in indegree.items() if deg == 0)
        order: list[str] = []
        while ready:
            tid = ready.pop(0)
            order.append(tid)
            newly_ready: list[str] = []
            for dependent in dependents[tid]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    newly_ready.append(dependent)
            for nxt in sorted(newly_ready):
                # Insert preserving sorted order of the ready frontier.
                ready.append(nxt)
            ready.sort()

        if len(order) != len(self._tasks):
            cyclic = sorted(set(self._tasks) - set(order))
            raise SchedulingError(
                f"dependency cycle detected among tasks: {', '.join(cyclic)}"
            )
        return order

    def topological_order(self) -> list[str]:
        """Return task ids in a valid execution order (dependencies first)."""
        return list(self._order)

    def mark_complete(self, task_id: str) -> None:
        """Record ``task_id`` as finished."""
        if task_id not in self._tasks:
            raise SchedulingError(f"cannot complete unknown task: {task_id}")
        self._complete.add(task_id)

    def is_complete(self, task_id: str) -> bool:
        """Return whether ``task_id`` has been marked complete."""
        return task_id in self._complete

    def newly_unblocked(self) -> list[SubTask]:
        """Return tasks that just became runnable, in topological order.

        A task is returned at most once across the DAG's lifetime: the first call
        yields the initial ready set (no dependencies), and subsequent calls yield
        tasks unblocked by intervening ``mark_complete`` calls.
        """
        unblocked: list[SubTask] = []
        for tid in self._order:
            if tid in self._released or tid in self._complete:
                continue
            task = self._tasks[tid]
            if all(dep in self._complete for dep in task.depends_on):
                unblocked.append(task)
        for task in unblocked:
            self._released.add(task.task_id)
        return unblocked

    def mark_released(self, task_id: str) -> None:
        """Mark ``task_id`` as already handed out so ``newly_unblocked`` skips it.

        Used when restoring persisted scheduler state: tasks that were previously
        dispatched or queued must not be re-emitted as freshly unblocked.
        """
        if task_id not in self._tasks:
            raise SchedulingError(f"cannot release unknown task: {task_id}")
        self._released.add(task_id)

    def remaining(self) -> list[str]:
        """Return ids of tasks not yet marked complete, in topological order."""
        return [tid for tid in self._order if tid not in self._complete]

    def all_complete(self) -> bool:
        """Return whether every task in the DAG has been marked complete."""
        return len(self._complete) == len(self._tasks)
