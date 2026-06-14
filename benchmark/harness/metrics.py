"""Result dataclasses for one runner (MAK or Traditional)."""

from __future__ import annotations

from dataclasses import dataclass, field

from harness.agents import Usage


@dataclass
class RunResult:
    """Everything measured for one runner over the workload."""

    label: str  # "MAK" | "Traditional (git worktrees)"
    wall_seconds: float
    usage: Usage  # total tokens + model calls across the run
    passed: float  # tests passing after the run (mean, possibly fractional, when averaged)
    total: int  # the workload's expected test count (denominator)
    conflicts: float  # registry merge conflicts hit (0 for MAK by construction)
    resolutions: float  # conflict-resolution model calls made (0 for MAK)
    per_agent_calls: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0
