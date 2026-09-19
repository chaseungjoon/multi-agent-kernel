"""A scripted agent with timing hooks that force an interleaving.

The scenarios need "B was dispatched before A committed" to happen on purpose,
not by luck. The agent answers from a script, but B's *first* call is held:
if A was dispatched alongside it, B waits until A's commit is on record, so B's
bundle is the pre-A snapshot and its result arrives after A committed. If the
kernel did not let A run beside B (it serialized them), there is nothing to
wait for and B answers at once.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from mak.core.logging import EventType, SessionLogger
from mak.core.types import NodeId, TaskBundle, TaskResult

Response = str | Callable[[TaskBundle], str]


class ScriptedAgent:
    """Answers each task from a script; ``retry`` answers any later attempt."""

    def __init__(
        self,
        first: Mapping[str, Mapping[str, Response]],
        retry: Mapping[str, Mapping[str, Response]],
    ) -> None:
        self._first = first
        self._retry = retry
        self._lock = threading.Lock()
        self.calls: dict[str, int] = {}
        self._holds: dict[str, Callable[[], None]] = {}

    def hold(self, task_id: str, wait: Callable[[], None]) -> None:
        """Run ``wait`` before answering ``task_id``'s first attempt."""
        self._holds[task_id] = wait

    def assign(self, adapter: object, task: object) -> TaskResult:
        """Answer one dispatched bundle from the script, honouring any hold."""
        bundle = task if isinstance(task, TaskBundle) else None
        assert bundle is not None
        with self._lock:
            attempt = self.calls.get(bundle.task_id, 0)
            self.calls[bundle.task_id] = attempt + 1
        if attempt == 0 and bundle.task_id in self._holds:
            self._holds[bundle.task_id]()
        script = self._first if attempt == 0 else self._retry
        answers = script.get(bundle.task_id) or self._first.get(bundle.task_id, {})
        sources = {
            NodeId(node): (answer(bundle) if callable(answer) else answer)
            for node, answer in answers.items()
            if NodeId(node) in bundle.target_nodes
        }
        return TaskResult(
            task_id=bundle.task_id,
            success=bool(sources),
            modified_nodes=list(sources),
            new_sources=sources,
        )

    def shutdown(self) -> None:  # parity with mak.AgentRunner
        """Nothing to shut down: the scripted agent owns no processes."""
        pass


def wait_for_peer(
    logger: SessionLogger, peer: str, *, grace_s: float = 0.3, timeout_s: float = 10.0
) -> Callable[[], None]:
    """Hold until ``peer`` completes, if it was dispatched concurrently at all."""

    def seen(event: EventType) -> bool:
        return any(
            e.event_type is event and e.payload.get("task_id") == peer
            for e in logger.read_log()
        )

    def wait() -> None:
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and not seen(EventType.TASK_DISPATCHED):
            time.sleep(0.005)
        if not seen(EventType.TASK_DISPATCHED):
            return  # the kernel did not run the peer beside us
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if (
                seen(EventType.TASK_COMPLETED)
                or seen(EventType.TASK_FAILED)
                or seen(EventType.COMMIT_DEFERRED)  # the peer waits on us
            ):
                return
            time.sleep(0.005)

    return wait
