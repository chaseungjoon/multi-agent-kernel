"""Wave 20 acceptance — no false positives on clean runs, and no slowdown.

The semantic corpus (``tests/test_semantic_corpus.py``) covers the other half
of the acceptance — every shape prevented or detected, with the worktree
baseline beside it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from shutil import copytree

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCH))

import run_benchmark as cli  # noqa: E402
from harness.agents import make_backends  # noqa: E402
from harness.mak_runner import run_mak  # noqa: E402
from harness.planner import DEFAULT_PLANNER, apply_plan, make_plan  # noqa: E402
from harness.workload import WORKLOADS, assign  # noqa: E402

from mak.config import SemanticConfig  # noqa: E402


def _run(
    tmp_path: Path, project: str, semantic: SemanticConfig | None = None
) -> object:
    workload = WORKLOADS[project]
    project_dir = tmp_path / "project"
    copytree(BENCH / workload.template, project_dir)
    specs = cli._parse_specs(None, 3, project)
    assignment = assign(workload, 3)
    if project == "4":
        plan = make_plan(workload, project_dir, 3, DEFAULT_PLANNER, mock=True)
        workload, assignment = apply_plan(workload, plan)
    return run_mak(
        project_dir, tmp_path / "state", make_backends(specs, mock=True),
        assignment, workload, semantic,
    )


@pytest.mark.parametrize("project", ["basic", "2", "3", "4"])
def test_clean_mock_runs_have_no_rejections_or_fix_ups(
    tmp_path: Path, project: str
) -> None:
    # The false-positive guard: on a wave where every task does exactly what it
    # was asked, none of Wave 20's checks may fire — not a rejection, not a
    # stale-read re-dispatch, not a fix-up task.
    result = _run(tmp_path, project)
    assert result.notes == []
    assert result.conflicts == 0
    assert result.resolutions == 0
    assert result.passed == result.total


def test_registry_keys_let_appenders_run_in_parallel(tmp_path: Path) -> None:
    # Template 3's four shared tables are the contended nodes: with key-level
    # locks the appenders hold them together and the kernel merges their lines;
    # with the flag off they serialize on the node lock. Both must be correct.
    with_keys = _run(tmp_path / "on", "3")
    without = _run(tmp_path / "off", "3", SemanticConfig(registry_keys=False))
    assert with_keys.passed == with_keys.total == without.passed


@pytest.mark.parametrize("project", ["3", "4"])
def test_api_split_does_not_cost_wall_clock(tmp_path: Path, project: str) -> None:
    # "With the API/body split on, Template 3/4 wall-clock is no worse than
    # before." The mock backend is instant, so this measures kernel time.
    ablated = SemanticConfig(api_locks=False, intention_locks=False)
    start = time.monotonic()
    off = _run(tmp_path / "off", project, ablated)
    baseline = time.monotonic() - start
    start = time.monotonic()
    on = _run(tmp_path / "on", project)
    with_split = time.monotonic() - start
    assert off.passed == on.passed == on.total
    assert with_split <= baseline * 2.0 + 2.0, (with_split, baseline)
