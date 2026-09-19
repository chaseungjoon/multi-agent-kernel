"""Acceptance tests for the zero-LLM simulated-agent scaling benchmark."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

from harness.synthetic_spec import (  # noqa: E402
    SyntheticSpec,
    assign_operations,
    build_workload,
    write_project,
)
from sim.backend import SimBackend  # noqa: E402
from sim.fit import CallSample, fit_profile, linear_fit  # noqa: E402
from sim.profile import load_profile  # noqa: E402
from sim.runtime import RunCase, run_case  # noqa: E402
from sweep import case_key, expand_cases, load_config  # noqa: E402


def _profile_path() -> Path:
    return BENCHMARK / "sim" / "profiles" / "default.json"


def test_common_random_numbers_ignore_backend_identity() -> None:
    """The same operation, seed, and attempt receive exactly the same sample."""
    profile = load_profile(_profile_path())
    operation = build_workload(
        SyntheticSpec(modules=1, tasks=1, shared_tables=1, seed=9)
    ).workload.operations[0]
    left = SimBackend("left", profile, seed=42, kernel_only=True)
    right = SimBackend("right", profile, seed=42, kernel_only=True)

    left_source, left_usage = left.implement(operation, "def stub(): pass")
    right_source, right_usage = right.implement(operation, "def stub(): pass")

    assert left_source == right_source == operation.reference
    assert left_usage == right_usage
    assert left.calls == right.calls


def test_synthetic_project_and_assignments_share_one_spec(tmp_path: Path) -> None:
    """Generated stubs, references, oracle size, and assignments cannot drift."""
    generated = build_workload(
        SyntheticSpec(
            modules=2,
            tasks=6,
            shared_tables=2,
            registration_probabilities=(0.0, 1.0, 0.0),
            seed=3,
        )
    )
    write_project(tmp_path, generated)

    assert len(generated.workload.operations) == 6
    assert generated.workload.expected_tests == 7
    assert (tmp_path / "synthetic" / "module_000.py").exists()
    assert (
        "test_all_registrations_survive"
        in (tmp_path / "tests" / "test_generated.py").read_text()
    )
    assert assign_operations(generated, 3, policy="round_robin") == [0, 1, 2, 0, 1, 2]
    avoiding = assign_operations(generated, 3, policy="conflict_avoiding")
    for table in generated.workload.shared_modules:
        owners = {
            avoiding[index]
            for index, operation in enumerate(generated.workload.operations)
            if table
            in {registration.module for registration in operation.registrations}
        }
        assert len(owners) <= 1


def test_profile_fit_reports_linear_tokens_and_beta_posterior() -> None:
    """Telemetry fitting produces usable implement and resolver profiles."""
    samples = [
        CallSample("implement", 1.0, 100, 20, 100, "test", "model"),
        CallSample("implement", 2.0, 200, 40, 200, "test", "model"),
    ]
    fit = linear_fit([100.0, 200.0], [100.0, 200.0])
    profile = fit_profile(samples, dropped=2, survived=8)

    assert fit.slope == pytest.approx(1.0)
    assert profile["resolver_drop_posterior"] == {"alpha": 3, "beta": 9}
    assert set(profile["calls"]) == {"implement", "resolve"}


def test_smoke_config_covers_all_required_arms_and_is_stable() -> None:
    """The checked-in smoke grid covers baselines/ablations with stable keys."""
    config = load_config(BENCHMARK / "sweeps" / "smoke.yaml")
    cases = expand_cases(config)

    assert {
        "sequential",
        "worktree_end",
        "worktree_often",
        "worktree_avoid",
        "mak_node",
        "mak_file",
        "mak_no_api",
        "mak_no_key",
    } <= {case.arm for case in cases}
    assert len({case_key(case) for case in cases}) == len(cases)
    assert case_key(cases[0]) == case_key(cases[0])


def test_tiny_case_runs_real_mak_without_model_calls(tmp_path: Path) -> None:
    """The simulated backend drives the production Session and writes its oracle."""
    case = RunCase(
        agents=2,
        tasks=3,
        modules=2,
        shared_tables=2,
        popularity="uniform",
        zipf_exponent=1.2,
        assignment="round_robin",
        contention_kinds=("registry_append",),
        arm="mak_node",
        seed=21,
        time_scale=0.0,
        failure_rate=0.0,
        kernel_only=True,
    )
    output = run_case(case, load_profile(_profile_path()), tmp_path / "run")

    assert output.metrics.accuracy == 1.0
    assert output.metrics.registrations_dropped == 0
    assert output.metrics.kernel_commit_p50_seconds > 0
    events = tmp_path / "run" / "mak_state" / "events.jsonl"
    assert any(
        json.loads(line)["event_type"] == "phase_span"
        for line in events.read_text().splitlines()
    )
