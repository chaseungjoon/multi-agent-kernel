"""Run the workload through the real MAK kernel.

This builds a genuine :class:`mak.session.Session` over a copy of the project and
installs one subtask per operation, targeting its function node **and** every
shared ``_register_all`` node it registers into (zero, one, or several — MAK's
atomic lock pre-allocation claims them together). Contended nodes serialize under
MAK's node-level write locks so no registration is ever lost; tasks touching
different shared tables (or none) run fully in parallel. The agent work itself is
delegated to the same backends the traditional runner uses.

Wave 20: each subtask *declares* what it is — a body-only edit of its function
(``changes_api=False``: implementing a stub keeps its signature) and the literal
key it registers in each shared table (``registry_keys``). With the kernel's
interface split and key-level registry locks on (the defaults), tasks appending
different keys to one table run in parallel and the kernel merges their lines;
``semantic`` turns either off for the ablation study.
"""

from __future__ import annotations

import ast
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import cast

from harness.agents import Backend, Usage
from harness.metrics import RunResult, measure_registration_survival
from harness.workload import Workload, add_registration, operation_by_func_node
from mak.config import (
    GitConfig,
    MakConfig,
    NodeStoreConfig,
    SemanticConfig,
    SessionConfig,
)
from mak.core.logging import EventType, SessionLogger
from mak.core.types import LockEntry, LockMode, NodeId, SubTask, TaskBundle, TaskResult
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

        # The subtask targets the op's function node first, then its shared nodes.
        func_node = str(bundle.target_nodes[0])
        op = operation_by_func_node(self._workload.operations, func_node)
        stub = bundle.context.get(f"write_source:{op.func_node}", "")
        func_source, usage = backend.implement(op, stub)

        new_sources: dict[NodeId, str] = {NodeId(op.func_node): func_source}
        for reg in op.registrations:
            node = op.shared_node(reg)
            current = bundle.context.get(f"write_source:{node}", "")
            new_sources[NodeId(node)] = add_registration(current, reg.line)

        with self._guard:
            self.usage = self.usage + usage
            self.calls_by_agent[backend.name] = (
                self.calls_by_agent.get(backend.name, 0) + usage.calls
            )

        return TaskResult(
            task_id=bundle.task_id,
            success=True,
            new_sources=new_sources,
        )

    def shutdown(self) -> None:  # parity with mak.AgentRunner
        pass


class _MeasuredLockTable:
    """Measure scheduler lock delay while delegating to the real lock table."""

    def __init__(self, inner: LockTable) -> None:
        self._inner = inner
        self._first_wait: dict[str, tuple[float, tuple[str, ...]]] = {}
        self.wait_seconds: list[float] = []
        self.wait_by_node: dict[str, float] = defaultdict(float)

    def try_acquire_all(
        self, requests: list[tuple[NodeId, LockMode]], holder: str
    ) -> bool:
        acquired = self._inner.try_acquire_all(requests, holder)
        now = time.perf_counter()
        nodes = tuple(str(node) for node, _mode in requests)
        if not acquired:
            self._first_wait.setdefault(holder, (now, nodes))
        elif holder in self._first_wait:
            started, waited_nodes = self._first_wait.pop(holder)
            elapsed = now - started
            self.wait_seconds.append(elapsed)
            share = elapsed / max(1, len(waited_nodes))
            for node in waited_nodes:
                self.wait_by_node[node] += share
        return acquired

    def release(self, node_id: NodeId, mode: LockMode, holder: str) -> bool:
        return self._inner.release(node_id, mode, holder)

    def release_all(self, holder: str) -> int:
        return self._inner.release_all(holder)

    def clear(self) -> int:
        return self._inner.clear()

    def expire_stale(self) -> list[LockEntry]:
        return self._inner.expire_stale()

    def holds_all(self, requests: list[tuple[NodeId, LockMode]], holder: str) -> bool:
        return self._inner.holds_all(requests, holder)

    def renew_all(self, holder: str) -> int:
        return self._inner.renew_all(holder)

    def all_entries(self) -> dict[NodeId, list[LockEntry]]:
        return self._inner.all_entries()


def _config(
    project_dir: Path,
    mak_dir: Path,
    num_agents: int,
    semantic: SemanticConfig | None = None,
) -> MakConfig:
    return MakConfig(
        session=SessionConfig(
            work_dir=str(project_dir),
            mak_dir=str(mak_dir),
            max_concurrent_agents=num_agents,
        ),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(exclude_patterns=_EXCLUDES),
        semantic=semantic or SemanticConfig(),
    )


def registration_key(line: str) -> str | None:
    """Return the literal first argument of a ``register("<key>", ...)`` line."""
    try:
        stmt = ast.parse(line.strip()).body[0]
    except (SyntaxError, IndexError):
        return None
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and stmt.value.args
        and isinstance(stmt.value.args[0], ast.Constant)
        and isinstance(stmt.value.args[0].value, str)
    ):
        return stmt.value.args[0].value
    return None


def run_mak(
    project_dir: Path,
    mak_dir: Path,
    backends: list[Backend],
    assignment: list[int],
    workload: Workload,
    semantic: SemanticConfig | None = None,
    *,
    granularity: str = "node",
) -> RunResult:
    """Implement the workload through MAK; return measured results."""
    by_name = {b.name: b for b in backends}
    runner = _BenchmarkRunner(by_name, workload)

    file_dependencies = _file_dependencies(workload) if granularity == "file" else {}
    subtasks = [
        SubTask(
            task_id=op.name,
            description=f"implement and register operation '{op.name}'",
            target_nodes=[
                NodeId(op.func_node),
                *(NodeId(op.shared_node(reg)) for reg in op.registrations),
            ],
            agent_type=backends[assignment[i]].name,
            changes_api=False,
            registry_keys={
                NodeId(op.shared_node(reg)): [key]
                for reg in op.registrations
                if (key := registration_key(reg.line)) is not None
            }
            if op.commutative_registrations
            else {},
            depends_on=list(
                dict.fromkeys((*op.depends_on, *file_dependencies.get(op.name, ())))
            ),
        )
        for i, op in enumerate(workload.operations)
    ]

    config = _config(project_dir, mak_dir, len(backends), semantic)
    logger = SessionLogger(mak_dir / "events.jsonl")
    measured_locks = _MeasuredLockTable(
        LockTable(default_timeout=config.session.lock_timeout_s)
    )
    session = Session(
        session_id="benchmark-mak",
        config=config,
        node_store=NodeStore(mak_dir / "node_store"),
        lock_table=measured_locks,
        registry=cast("object", _Registry()),  # type: ignore[arg-type]
        agent_runner=runner,
        logger=logger,
    )
    session.initialize()
    session.install_plan(subtasks)

    start = time.monotonic()
    result = session.run()
    elapsed = time.monotonic() - start

    print(
        "[mak] agents done; measuring accuracy (pytest) ...",
        file=sys.stderr,
        flush=True,
    )
    passed = _measure(project_dir)
    survival = measure_registration_survival(project_dir, workload)
    commit_spans = [
        _duration(entry.payload.get("duration_seconds"))
        for entry in logger.read_log()
        if entry.event_type is EventType.PHASE_SPAN
        and entry.payload.get("phase") == "validate_commit_reconstruct"
    ]
    store_bytes = sum(
        path.stat().st_size for path in mak_dir.rglob("*") if path.is_file()
    )
    modeled_seconds = max(
        (float(getattr(backend, "modeled_seconds", 0.0)) for backend in backends),
        default=0.0,
    )
    notes = [] if result.ok else [f"MAK run state: {result.state.value}"]
    # Wave 20: a clean run must leave nothing behind — no commit rejected, no
    # fix-up work detected. Both are reported rather than assumed: "0 conflicts
    # by construction" was true of textual conflicts only.
    fixups = session.detect_cascade_tasks()
    if fixups:
        notes.append(
            f"MAK detected {len(fixups)} fix-up task(s): "
            + ", ".join(t.task_id for t in fixups[:5])
        )
    return RunResult(
        label="MAK (shared-memory kernel)",
        wall_seconds=elapsed,
        usage=runner.usage,
        passed=passed,
        total=workload.expected_tests,
        conflicts=result.metrics.get("conflict_rejections", 0.0),
        resolutions=result.metrics.get("stale_redispatches", 0.0),
        per_agent_calls=runner.calls_by_agent,
        notes=notes,
        registration_expected=survival.expected,
        registration_survived=survival.survived,
        registration_dropped=survival.dropped,
        registration_duplicates=survival.duplicates,
        kernel_seconds=sum(commit_spans),
        kernel_commit_seconds=commit_spans,
        lock_wait_seconds=measured_locks.wait_seconds,
        top_waited_nodes=dict(
            sorted(
                measured_locks.wait_by_node.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:10]
        ),
        store_bytes=store_bytes,
        modeled_agent_seconds=modeled_seconds,
        unscaled_seconds=modeled_seconds + sum(commit_spans),
    )


def _file_dependencies(workload: Workload) -> dict[str, tuple[str, ...]]:
    """Serialize tasks sharing any file for the file-lock ablation."""
    previous: dict[str, str] = {}
    dependencies: dict[str, tuple[str, ...]] = {}
    for operation in workload.operations:
        resources = {f"{workload.package}/{operation.module}.py"}
        resources.update(
            f"{workload.package}/{registration.module}.py"
            for registration in operation.registrations
        )
        deps = tuple(
            dict.fromkeys(
                previous[resource]
                for resource in sorted(resources)
                if resource in previous
            )
        )
        dependencies[operation.name] = deps
        for resource in resources:
            previous[resource] = operation.name
    return dependencies


def _duration(value: object) -> float:
    """Narrow a structured event duration to a floating-point number."""
    return float(value) if isinstance(value, int | float) else 0.0


def _measure(project_dir: Path) -> int:
    from harness.accuracy import measure

    return measure(project_dir)
