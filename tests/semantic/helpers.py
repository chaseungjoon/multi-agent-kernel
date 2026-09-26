"""Shared fakes for Wave 20 session tests: a scripted, interleaving agent."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from mak.config import (
    GitConfig,
    MakConfig,
    NodeStoreConfig,
    PlannerConfig,
    SemanticConfig,
    SessionConfig,
)
from mak.core.logging import EventType, LogEntry, SessionLogger
from mak.core.types import NodeId, SubTask, TaskBundle, TaskResult
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.session import Session

# A response is either fixed source or computed from the bundle it answers.
Response = str | Callable[[TaskBundle], str]


class FakeAdapter:
    agent_type = "fake"


class FakeRegistry:
    def get(self, agent_type: str) -> FakeAdapter:
        return FakeAdapter()


class ScriptRunner:
    """Returns scripted sources per task and attempt, optionally held back.

    ``script[task_id]`` is a list of attempts, each ``{node_id: Response}``; the
    last attempt repeats. ``hold[task_id]`` is a predicate the agent call waits
    on before answering — this is how a test forces "B was dispatched before A
    committed": B holds until A's commit is visible in the store.
    """

    def __init__(
        self,
        script: dict[str, list[dict[str, Response]]],
        hold: dict[str, Callable[[], bool]] | None = None,
        *,
        timeout_s: float = 10.0,
    ) -> None:
        self._script = script
        self._hold = hold or {}
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self.calls: dict[str, int] = {}
        self.bundles: list[TaskBundle] = []

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        with self._lock:
            attempt = self.calls.get(task.task_id, 0)
            self.calls[task.task_id] = attempt + 1
            self.bundles.append(task)
        predicate = self._hold.get(task.task_id)
        if predicate is not None and attempt == 0:
            _wait_for(predicate, self._timeout)
        attempts = self._script.get(task.task_id, [{}])
        responses = attempts[min(attempt, len(attempts) - 1)]
        sources = {
            NodeId(node): (resp(task) if callable(resp) else resp)
            for node, resp in responses.items()
        }
        return TaskResult(
            task_id=task.task_id,
            success=bool(sources),
            modified_nodes=list(sources),
            new_sources=sources,
        )

    def bundles_for(self, task_id: str) -> list[TaskBundle]:
        return [b for b in self.bundles if b.task_id == task_id]


def _wait_for(predicate: Callable[[], bool], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("scripted agent timed out waiting for its interleaving")


def committed(store: NodeStore, node_id: str, needle: str) -> Callable[[], bool]:
    """Predicate: the node's committed source contains ``needle``."""

    def check() -> bool:
        try:
            return needle in store.get_node(NodeId(node_id)).source
        except Exception:  # noqa: BLE001 - absent node reads as not yet
            return False

    return check


def logged(
    logger: SessionLogger, event: EventType, **match: object
) -> Callable[[], bool]:
    """Predicate: an event with these payload values has been logged."""

    def check() -> bool:
        return any(
            e.event_type is event
            and all(e.payload.get(k) == v for k, v in match.items())
            for e in logger.read_log()
        )

    return check


def make_session(
    tmp_path: Path,
    runner: object,
    *,
    semantic: SemanticConfig | None = None,
    max_attempts: int = 3,
    concurrency: int = 4,
    validate: bool = True,
    gate_runner: object | None = None,
    adjudicator_llm: object | None = None,
) -> tuple[Session, NodeStore, SessionLogger]:
    """Return a session over ``tmp_path``, with a logger and this config."""
    store = NodeStore(tmp_path / ".mak" / "store")
    logger = SessionLogger(tmp_path / ".mak" / "log.jsonl")
    config = MakConfig(
        session=SessionConfig(
            work_dir=str(tmp_path),
            mak_dir=str(tmp_path / ".mak"),
            max_concurrent_agents=concurrency,
        ),
        planner=PlannerConfig(validate=validate),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(),
        semantic=semantic or SemanticConfig(),
    )
    session = Session(
        session_id="w20",
        config=config,
        node_store=store,
        lock_table=LockTable(),
        registry=FakeRegistry(),  # type: ignore[arg-type]
        agent_runner=runner,  # type: ignore[arg-type]
        logger=logger,
        max_attempts=max_attempts,
        collect_timeout_s=20.0,
        gate_runner=gate_runner,  # type: ignore[arg-type]
        adjudicator_llm=adjudicator_llm,  # type: ignore[arg-type]
    )
    return session, store, logger


def task(
    task_id: str,
    targets: list[str],
    *,
    context: list[str] | None = None,
    deps: list[str] | None = None,
    **declared: object,
) -> SubTask:
    return SubTask(
        task_id=task_id,
        description=f"task {task_id}",
        target_nodes=[NodeId(n) for n in targets],
        context_nodes=[NodeId(n) for n in context or []],
        depends_on=list(deps or []),
        **declared,  # type: ignore[arg-type]
    )


def events(logger: SessionLogger, event: EventType) -> list[LogEntry]:
    return [e for e in logger.read_log() if e.event_type is event]
