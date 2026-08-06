"""The post-wave fix-up loop, shared by every front end.

After a wave, ``Session.detect_cascade_tasks`` reports the work that wave left
behind: existing callers a committed signature change broke, and modules the wave
created that disagree about each other's API. Those become a new plan, reviewed
like any other, and run as another wave — repeating until nothing is left.

The loop lives here rather than in a front end because it was in one: the CLI ran
it and the interactive app did not, so a defect the kernel could name was reported
or not depending on which entry point the operator happened to launch. A guard that
runs on one of two front ends is not a guard. The front ends supply *presentation*
(``announce``) and *approval* (``approve``); neither owns when a fix-up wave runs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from mak.core.types import SubTask

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mak.session import SessionResult

# Return the (possibly edited) tasks to run, or None to decline the wave.
CascadeApproval = Callable[[list[SubTask]], "list[SubTask] | None"]
# Called once per detected batch, before approval, with the tasks found.
CascadeAnnounce = Callable[[list[SubTask]], None]


class _CascadingSession(Protocol):
    """The slice of ``Session`` this loop drives."""

    def detect_cascade_tasks(self) -> list[SubTask]: ...

    def install_plan(self, subtasks: list[SubTask]) -> None: ...

    def run(self, max_iterations: int = ...) -> SessionResult: ...


def run_cascade_waves(
    session: _CascadingSession,
    approve: CascadeApproval,
    *,
    announce: CascadeAnnounce | None = None,
    max_waves: int = 10,
) -> SessionResult | None:
    """Run fix-up waves until the session reports nothing left, or one is declined.

    Returns the result of the last wave actually run, or ``None`` when no cascade
    was detected — so a caller can keep reporting its original result unchanged.

    ``max_waves`` bounds a pathological loop where each fix-up wave produces
    another batch of defects. Reaching it is not an error the loop can resolve; it
    stops and lets the caller report what it has.
    """
    result: SessionResult | None = None
    for _wave in range(max_waves):
        tasks = session.detect_cascade_tasks()
        if not tasks:
            break
        if announce is not None:
            announce(tasks)
        approved = approve(tasks)
        if approved is None:
            break
        session.install_plan(approved)
        result = session.run()
    return result
