"""What happened to the user's *request*, as distinct from the last wave.

A run is not one wave. An initial wave can leave callers broken; a cascade wave
fixes those; either can fail, be declined, or hit the loop's ceiling. Both front
ends nevertheless did this::

    cascade_result = run_cascade_waves(...)
    if cascade_result is not None:
        result = cascade_result

— replacing the original result with the last one. Combined with ``install_plan``
resetting the session's per-wave accumulators, that made an initial wave with a
failed task plus a successful fix-up wave indistinguishable from a clean run:
green tally, zero failures, exit code 0, and a push if ``auto_push`` was on.

:class:`ExecutionResult` is the aggregate those front ends should have been
reporting. It keeps every wave and answers the two questions separately:

* **how much work succeeded** — ``tasks_completed`` and friends, summed across
  waves, which is a statistic; and
* **was the request satisfied** — :attr:`request_satisfied`, which is a verdict,
  and the only thing that should gate an exit code or a push.

Those are different claims and conflating them is what made the reporting
dishonest. Four tasks completing across two waves is not the same as the user
getting what they asked for, and a run can be busy and still have failed.

**An earlier failure is never cleared by later work.** A cascade wave is not a
retry of a failed task — it is new work about the callers a *successful* change
broke — so it has no standing to resolve one. If some later wave genuinely fixes
an earlier failure, that is for whoever ties the two together to say; the
aggregate will not infer it.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from mak.cascade import CascadeOutcome

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for typing only, the same way ``mak.cascade`` does it: the session
    # is what *produces* these results, so importing it at runtime would make the
    # aggregate depend on the thing that reports through it.
    from mak.session import SessionResult

# A task id, tagged with the wave it belongs to, so two waves that both produce
# a task called "fix-1" stay distinguishable in an aggregate listing.
WaveTask = tuple[int, str]


@dataclasses.dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The initial wave plus every cascade wave, reported as one outcome."""

    initial: SessionResult
    cascade: CascadeOutcome = dataclasses.field(default_factory=CascadeOutcome)

    @property
    def waves(self) -> tuple[SessionResult, ...]:
        """Every wave that ran, initial first."""
        return (self.initial, *self.cascade.waves)

    @property
    def wave_count(self) -> int:
        """How many waves ran in total."""
        return len(self.waves)

    def _collect(self, field_name: str) -> tuple[WaveTask, ...]:
        return tuple(
            (index, task_id)
            for index, result in enumerate(self.waves)
            for task_id in getattr(result, field_name)
        )

    @property
    def completed(self) -> tuple[WaveTask, ...]:
        """Every task that completed, across every wave."""
        return self._collect("completed")

    @property
    def failed(self) -> tuple[WaveTask, ...]:
        """Every task that failed, across every wave. Later work never clears these."""
        return self._collect("failed")

    @property
    def blocked(self) -> tuple[WaveTask, ...]:
        """Every task stranded with no failed ancestor to explain it."""
        return self._collect("blocked")

    @property
    def skipped(self) -> tuple[WaveTask, ...]:
        """Every task skipped because something it depended on failed."""
        return self._collect("skipped")

    @property
    def noop(self) -> tuple[WaveTask, ...]:
        """Every task that closed because the agent asserted nothing needed changing."""
        return self._collect("noop")

    @property
    def tasks_completed(self) -> int:
        """How many tasks completed. A statistic, **not** a verdict."""
        return len(self.completed)

    @property
    def failure_reasons(self) -> dict[WaveTask, str]:
        """Why each failed task failed, keyed the same way ``failed`` is."""
        return {
            (index, task_id): reason
            for index, result in enumerate(self.waves)
            for task_id, reason in result.failure_reasons.items()
        }

    @property
    def stopped_reasons(self) -> tuple[tuple[int, str], ...]:
        """Per-wave reasons the kernel itself halted a wave (today: the budget)."""
        return tuple(
            (index, result.stopped_reason)
            for index, result in enumerate(self.waves)
            if result.stopped_reason is not None
        )

    @property
    def request_satisfied(self) -> bool:
        """Whether the user actually got what they asked for.

        Every wave clean, no fix-up wave declined, the loop not stopped at its
        ceiling, and nothing left outstanding. This — not a completed-task count
        — is what gates the exit code and the push.
        """
        return all(result.ok for result in self.waves) and self.cascade.clean

    @property
    def unresolved(self) -> tuple[str, ...]:
        """Cascade defects still outstanding when the run stopped."""
        return self.cascade.unresolved

    def summary_line(self) -> str:
        """One line of tallies, for a front end that wants the numbers only."""
        parts = [f"{self.tasks_completed} completed"]
        if self.noop:
            parts.append(f"{len(self.noop)} no-op")
        parts.append(f"{len(self.failed)} failed")
        parts.append(f"{len(self.skipped)} skipped")
        parts.append(f"{len(self.blocked)} blocked")
        if self.wave_count > 1:
            parts.append(f"across {self.wave_count} waves")
        return ", ".join(parts)
