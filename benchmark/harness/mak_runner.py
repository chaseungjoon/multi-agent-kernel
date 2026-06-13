"""Run the workload through the real MAK kernel.

This builds a genuine :class:`mak.session.Session` over a copy of the project and
installs one subtask per operation, each targeting its function node **and** the
shared ``_register_all`` node. Because every task contends on that one registry
node, MAK's node-level write lock serializes the registry edits — so every
registration is preserved and there is nothing to merge. The agent work itself is
delegated to the same backends the traditional runner uses.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import cast

from harness.agents import Backend, Usage
from harness.metrics import RunResult
from harness.workload import (
    REGISTRY_NODE,
    Workload,
    add_registration,
    operation_by_func_node,
)
from mak.config import GitConfig, MakConfig, NodeStoreConfig, SessionConfig
from mak.core.types import NodeId, SubTask, TaskBundle, TaskResult
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.session import Session

_EXCLUDES = ("**/tests/**", "**/conftest.py", "**/__pycache__/**")


class _Adapter:
    def __init__(self, agent_type: str) -> None:
        self.agent_type = agent_type


class _Registry:
    """Hands the session an adapter that simply carries the agent (model) name."""

    def get(self, agent_type: str) -> _Adapter:
        return _Adapter(agent_type)


class _BenchmarkRunner:
    """MAK agent runner: delegates each task to the backend named by its agent_type."""

    def __init__(self, backends: dict[str, Backend], workload: Workload) -> None:
        self._backends = backends
        self._workload = workload
        self._guard = threading.Lock()
        self.usage = Usage()
        self.calls_by_agent: dict[str, int] = {}

    def assign(self, adapter: object, task: object) -> object:
        bundle = cast(TaskBundle, task)
        agent_type = getattr(adapter, "agent_type", "")
        backend = self._backends[agent_type]

        func_node = next(n for n in bundle.target_nodes if str(n) != REGISTRY_NODE)
        op = operation_by_func_node(self._workload.operations, str(func_node))
        stub = bundle.context.get(f"write_source:{op.func_node}", "")
        func_source, usage = backend.implement(op, stub)

        current_registry = bundle.context.get(f"write_source:{REGISTRY_NODE}", "")
        new_registry = add_registration(current_registry, op.register_line)

        with self._guard:
            self.usage = self.usage + usage
            self.calls_by_agent[backend.name] = (
                self.calls_by_agent.get(backend.name, 0) + usage.calls
            )

        return TaskResult(
            task_id=bundle.task_id,
            success=True,
            new_sources={
                NodeId(op.func_node): func_source,
                NodeId(REGISTRY_NODE): new_registry,
            },
        )

    def shutdown(self) -> None:  # parity with mak.AgentRunner
        pass


def _config(project_dir: Path, mak_dir: Path, num_agents: int) -> MakConfig:
    return MakConfig(
        session=SessionConfig(
            work_dir=str(project_dir),
            mak_dir=str(mak_dir),
            max_concurrent_agents=num_agents,
        ),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(exclude_patterns=_EXCLUDES),
    )


def run_mak(
    project_dir: Path,
    mak_dir: Path,
    backends: list[Backend],
    assignment: list[int],
    workload: Workload,
) -> RunResult:
    """Implement the workload through MAK; return measured results."""
    by_name = {b.name: b for b in backends}
    runner = _BenchmarkRunner(by_name, workload)

    subtasks = [
        SubTask(
            task_id=op.name,
            description=f"implement and register operation '{op.name}'",
            target_nodes=[NodeId(op.func_node), NodeId(REGISTRY_NODE)],
            agent_type=backends[assignment[i]].name,
        )
        for i, op in enumerate(workload.operations)
    ]

    config = _config(project_dir, mak_dir, len(backends))
    session = Session(
        session_id="benchmark-mak",
        config=config,
        node_store=NodeStore(mak_dir / "node_store"),
        lock_table=LockTable(default_timeout=config.session.lock_timeout_s),
        registry=cast("object", _Registry()),  # type: ignore[arg-type]
        agent_runner=runner,
    )
    session.initialize()
    session.install_plan(subtasks)

    start = time.monotonic()
    result = session.run()
    elapsed = time.monotonic() - start

    print("[mak] agents done; measuring accuracy (pytest) ...", file=sys.stderr, flush=True)
    passed = _measure(project_dir)
    notes = [] if result.ok else [f"MAK run state: {result.state.value}"]
    return RunResult(
        label="MAK (shared-memory kernel)",
        wall_seconds=elapsed,
        usage=runner.usage,
        passed=passed,
        total=workload.expected_tests,
        conflicts=0,
        resolutions=0,
        per_agent_calls=runner.calls_by_agent,
        notes=notes,
    )


def _measure(project_dir: Path) -> int:
    from harness.accuracy import measure

    return measure(project_dir)
