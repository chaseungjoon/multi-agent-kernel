"""Fan agent calls out to a thread pool and queue their results for collection."""

from __future__ import annotations

import queue
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import cast

from mak.core.types import TaskBundle, TaskResult
from mak.session.types import Assigner


@dataclass(frozen=True, slots=True)
class Completion:
    """One finished agent call: the bundle that was dispatched and its result."""

    bundle: TaskBundle
    result: TaskResult


@dataclass(frozen=True, slots=True)
class Dispatch:
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


class ConcurrentRunner:
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
        inner: Assigner,
        executor: ThreadPoolExecutor,
        completions: queue.Queue[Completion],
        enrich: Callable[[TaskBundle], Dispatch],
    ) -> None:
        self._inner = inner
        self._executor = executor
        self._completions = completions
        self._enrich = enrich

    def assign(self, adapter: object, task: object) -> object:
        """Enrich ``task`` and submit it, or queue its refusal when starved."""
        dispatch = self._enrich(cast(TaskBundle, task))
        if dispatch.starved_reason is not None:
            # Never spend a model call on a bundle the kernel knows is empty:
            # queue the failure directly so it flows through the normal reporting
            # path, unretryable because a re-dispatch would build the same bundle.
            self._completions.put(Completion(
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
        self._completions.put(Completion(bundle, result))
