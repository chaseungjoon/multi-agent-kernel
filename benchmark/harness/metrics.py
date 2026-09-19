"""Result dataclasses for one runner (MAK or Traditional)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from harness.agents import Usage

if TYPE_CHECKING:
    from harness.workload import Workload


@dataclass(frozen=True, slots=True)
class RegistrationSurvival:
    """Expected shared-table lines that survived a completed run."""

    expected: int
    survived: int
    dropped: int
    duplicates: int


def measure_registration_survival(
    project_dir: Path, workload: Workload
) -> RegistrationSurvival:
    """Compare generated registration lines with the final shared tables."""
    expected_lines = {
        (registration.module, registration.line.strip())
        for operation in workload.operations
        for registration in operation.registrations
    }
    survived = 0
    duplicates = 0
    for module, line in expected_lines:
        path = project_dir / workload.package / f"{module}.py"
        source = path.read_text() if path.exists() else ""
        occurrences = sum(
            candidate.strip() == line for candidate in source.splitlines()
        )
        survived += int(occurrences > 0)
        duplicates += max(0, occurrences - 1)
    expected = len(expected_lines)
    return RegistrationSurvival(
        expected=expected,
        survived=survived,
        dropped=expected - survived,
        duplicates=duplicates,
    )


@dataclass
class RunResult:
    """Everything measured for one runner over the workload."""

    label: str  # "MAK" | "Traditional (git worktrees)"
    wall_seconds: float
    usage: Usage  # total tokens + model calls across the run
    passed: (
        float  # tests passing after the run (mean, possibly fractional, when averaged)
    )
    total: int  # the workload's expected test count (denominator)
    conflicts: float  # registry merge conflicts hit (0 for MAK by construction)
    resolutions: float  # conflict-resolution model calls made (0 for MAK)
    per_agent_calls: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    planning_usage: Usage = field(default_factory=Usage)
    planning_seconds: float = 0.0
    registration_expected: int = 0
    registration_survived: int = 0
    registration_dropped: int = 0
    registration_duplicates: int = 0
    kernel_seconds: float = 0.0
    kernel_commit_seconds: list[float] = field(default_factory=list)
    lock_wait_seconds: list[float] = field(default_factory=list)
    top_waited_nodes: dict[str, float] = field(default_factory=dict)
    store_bytes: int = 0
    modeled_agent_seconds: float = 0.0
    unscaled_seconds: float = 0.0

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0
