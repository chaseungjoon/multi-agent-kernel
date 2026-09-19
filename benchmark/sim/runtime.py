"""Execute one process-isolated synthetic benchmark arm."""

from __future__ import annotations

import dataclasses
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harness.agents import Backend
from harness.mak_runner import run_mak
from harness.metrics import RunResult
from harness.synthetic_spec import (
    SyntheticSpec,
    assign_operations,
    build_workload,
    write_project,
)
from harness.traditional import run_traditional

from mak.config import SemanticConfig
from sim.backend import SimBackend
from sim.profile import SimulationProfile


@dataclass(frozen=True, slots=True)
class RunCase:
    """One point in the sweep grid."""

    agents: int
    tasks: int
    modules: int
    shared_tables: int
    popularity: str
    zipf_exponent: float
    assignment: str
    contention_kinds: tuple[str, ...]
    arm: str
    seed: int
    time_scale: float
    failure_rate: float
    kernel_only: bool


@dataclass(frozen=True, slots=True)
class CaseMetrics:
    """Serializable measurements for a single arm."""

    makespan_seconds: float
    measured_wall_seconds: float
    unscaled_seconds: float
    modeled_agent_seconds: float
    agent_utilization: float
    critical_path_seconds: float
    conflicts: float
    resolver_calls: float
    tokens_in: int
    tokens_out: int
    model_calls: int
    oracle_passed: float
    oracle_total: int
    accuracy: float
    registrations_expected: int
    registrations_survived: int
    registrations_dropped: int
    registration_duplicates: int
    kernel_seconds: float
    kernel_commit_p50_seconds: float
    kernel_commit_p95_seconds: float
    lock_wait_p50_seconds: float
    lock_wait_p95_seconds: float
    top_waited_nodes: dict[str, float]
    store_bytes: int
    commits_per_second: float


@dataclass(frozen=True, slots=True)
class CaseOutput:
    """Case parameters, result metrics, and deterministic sample fingerprint."""

    case: RunCase
    metrics: CaseMetrics
    modeled_samples: tuple[tuple[str, str, int, float, int, int], ...]


def run_case(
    case: RunCase,
    profile: SimulationProfile,
    run_root: Path,
) -> CaseOutput:
    """Generate a project and execute exactly one real coordination arm."""
    if run_root.exists():
        shutil.rmtree(run_root)
    template = run_root / "template"
    spec = SyntheticSpec(
        modules=case.modules,
        tasks=case.tasks,
        shared_tables=case.shared_tables,
        popularity=case.popularity,
        zipf_exponent=case.zipf_exponent,
        contention_kinds=case.contention_kinds,
        assignment_policy=case.assignment,
        seed=case.seed,
    )
    generated = build_workload(spec)
    write_project(template, generated)
    project = run_root / "project"
    shutil.copytree(template, project)

    effective_agents = 1 if case.arm == "sequential" else case.agents
    policy = "conflict_avoiding" if case.arm == "worktree_avoid" else case.assignment
    assignment = assign_operations(generated, effective_agents, policy=policy)
    backends = [
        SimBackend(
            f"sim-agent-{index}",
            profile,
            seed=case.seed,
            time_scale=case.time_scale,
            failure_rate=case.failure_rate,
            kernel_only=case.kernel_only,
        )
        for index in range(effective_agents)
    ]

    started = time.perf_counter()
    result = _run_arm(
        case,
        project,
        run_root,
        backends,
        assignment,
        generated.workload,
    )
    measured_wall = time.perf_counter() - started
    samples = tuple(
        sorted(
            (
                call.kind,
                call.key,
                call.attempt,
                round(call.latency_seconds, 9),
                call.usage.tokens_in,
                call.usage.tokens_out,
            )
            for backend in backends
            for call in backend.calls
        )
    )
    return CaseOutput(
        case=case,
        metrics=_metrics(result, backends, measured_wall, case),
        modeled_samples=samples,
    )


def git_sha(repository: Path) -> str:
    """Return the benchmarked MAK revision, including a dirty marker."""
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return f"{sha}-dirty" if dirty else sha


def _run_arm(
    case: RunCase,
    project: Path,
    run_root: Path,
    backends: list[SimBackend],
    assignment: list[int],
    workload: object,
) -> RunResult:
    from harness.workload import Workload

    typed_workload = workload
    if not isinstance(typed_workload, Workload):
        raise TypeError("generated workload has an unexpected type")
    if case.arm in {"sequential", "worktree_end", "worktree_avoid"}:
        return run_traditional(
            project,
            run_root / "worktrees",
            cast("list[Backend]", backends),
            assignment,
            typed_workload,
            strategy="merge_at_end",
        )
    if case.arm == "worktree_often":
        return run_traditional(
            project,
            run_root / "worktrees",
            cast("list[Backend]", backends),
            assignment,
            typed_workload,
            strategy="merge_often",
        )
    semantic = SemanticConfig(
        api_locks=case.arm != "mak_no_api",
        registry_keys=case.arm != "mak_no_key",
    )
    granularity = "file" if case.arm == "mak_file" else "node"
    if case.arm not in {"mak_node", "mak_file", "mak_no_api", "mak_no_key"}:
        raise ValueError(f"unknown benchmark arm: {case.arm}")
    return run_mak(
        project,
        run_root / "mak_state",
        cast("list[Backend]", backends),
        assignment,
        typed_workload,
        semantic,
        granularity=granularity,
    )


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * quantile))
    return ordered[index]


def _metrics(
    result: RunResult,
    backends: list[SimBackend],
    measured_wall: float,
    case: RunCase,
) -> CaseMetrics:
    per_agent = [backend.modeled_seconds for backend in backends]
    total_agent = sum(per_agent)
    kernel = result.kernel_seconds
    if case.kernel_only or case.time_scale == 0:
        critical_path = 0.0
        unscaled = result.wall_seconds
    else:
        resolver_seconds = sum(
            call.latency_seconds
            for backend in backends
            for call in backend.calls
            if call.kind == "resolve"
        )
        # Traditional records the complete real merge phase. Remove the scaled
        # resolver sleeps before unscaling and leave actual git time untouched.
        if result.label.startswith("Traditional"):
            kernel = max(0.0, kernel - resolver_seconds * case.time_scale)
        scaled_agent_path = max(0.0, result.wall_seconds - kernel)
        critical_path = scaled_agent_path / case.time_scale
        unscaled = critical_path + kernel
    utilization = (
        total_agent / (len(backends) * critical_path)
        if backends and critical_path
        else 0.0
    )
    return CaseMetrics(
        makespan_seconds=unscaled,
        measured_wall_seconds=measured_wall,
        unscaled_seconds=unscaled,
        modeled_agent_seconds=total_agent,
        agent_utilization=utilization,
        critical_path_seconds=critical_path,
        conflicts=result.conflicts,
        resolver_calls=result.resolutions,
        tokens_in=result.usage.tokens_in,
        tokens_out=result.usage.tokens_out,
        model_calls=result.usage.calls,
        oracle_passed=result.passed,
        oracle_total=result.total,
        accuracy=result.accuracy,
        registrations_expected=result.registration_expected,
        registrations_survived=result.registration_survived,
        registrations_dropped=result.registration_dropped,
        registration_duplicates=result.registration_duplicates,
        kernel_seconds=kernel,
        kernel_commit_p50_seconds=(
            statistics.median(result.kernel_commit_seconds)
            if result.kernel_commit_seconds
            else 0.0
        ),
        kernel_commit_p95_seconds=_percentile(result.kernel_commit_seconds, 0.95),
        lock_wait_p50_seconds=(
            statistics.median(result.lock_wait_seconds)
            if result.lock_wait_seconds
            else 0.0
        ),
        lock_wait_p95_seconds=_percentile(result.lock_wait_seconds, 0.95),
        top_waited_nodes=result.top_waited_nodes,
        store_bytes=result.store_bytes,
        commits_per_second=(case.tasks / kernel if kernel else 0.0),
    )


def output_to_dict(output: CaseOutput) -> dict[str, object]:
    """Convert a typed output into JSON-compatible values."""
    return dataclasses.asdict(output)
