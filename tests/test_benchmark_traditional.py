import sys
from pathlib import Path
from shutil import copytree

import pytest

BENCHMARK_DIR = Path(__file__).resolve().parents[1] / "benchmark"
sys.path.insert(0, str(BENCHMARK_DIR))

from harness.agents import Usage  # noqa: E402
from harness.traditional import run_traditional  # noqa: E402
from harness.workload import WORKLOADS, Operation, assign  # noqa: E402


class _FailingBackend:
    """A backend whose calls all fail before changing a worktree."""

    name = "failing"

    def implement(self, op: Operation, stub_source: str) -> tuple[str, Usage]:
        raise RuntimeError("simulated provider outage")

    def resolve(self, versions: list[str]) -> tuple[str, Usage]:
        raise RuntimeError("simulated provider outage")


def test_traditional_runner_commits_empty_failed_agent_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / "project"
    copytree(BENCHMARK_DIR / "project_template_3", project_dir)
    workload = WORKLOADS["3"]
    backends = [_FailingBackend(), _FailingBackend(), _FailingBackend()]
    monkeypatch.setattr("harness.traditional.measure", lambda _: 0)

    result = run_traditional(
        project_dir,
        tmp_path / "worktrees",
        backends,
        assign(workload, len(backends)),
        workload,
    )

    assert result.usage.calls == 0
    assert len(result.notes) == len(workload.operations)
