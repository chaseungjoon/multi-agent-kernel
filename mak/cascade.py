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

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from mak.core.types import SubTask

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mak.session import SessionResult

# Return the (possibly edited) tasks to run, or None to decline the wave.
CascadeApproval = Callable[[list[SubTask]], "list[SubTask] | None"]
# Called once per detected batch, before approval, with the tasks found.
CascadeAnnounce = Callable[[list[SubTask]], None]


@dataclasses.dataclass(frozen=True, slots=True)
class CascadeOutcome:
    """Everything the fix-up loop did, and every way it stopped short.

    The loop used to return only its *last* ``SessionResult`` — or ``None`` — and
    both front ends then assigned that over the original result. So an initial
    wave with a failed task plus a successful fix-up wave was reported as a clean
    success, and the two ways the loop can stop without finishing had no
    representation at all: hitting ``max_waves`` returned the last successful
    result with nothing marking the limit, and a declined wave returned whatever
    happened to be there.

    Each of those is now a field. ``waves`` keeps every result rather than the
    last, because they are different claims about different work; ``declined``,
    ``limit_reached`` and ``unresolved`` say why the loop stopped, so a caller can
    tell "nothing was left to do" from "something was left and we stopped anyway".
    """

    waves: tuple[SessionResult, ...] = ()
    # The user was shown a fix-up wave and said no.
    declined: bool = False
    # ``max_waves`` was exhausted while defects were still being produced.
    limit_reached: bool = False
    # Cascade task ids still outstanding when the loop stopped. Empty when it
    # stopped because there was genuinely nothing left.
    unresolved: tuple[str, ...] = ()

    @property
    def ran(self) -> bool:
        """Whether any fix-up wave actually ran."""
        return bool(self.waves)

    @property
    def clean(self) -> bool:
        """Whether the loop finished with nothing outstanding and nothing refused."""
        return (
            not self.declined
            and not self.limit_reached
            and not self.unresolved
            and all(result.ok for result in self.waves)
        )


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
) -> CascadeOutcome:
    """Run fix-up waves until the session reports nothing left, or one is declined.

    Always returns a :class:`CascadeOutcome`, never ``None``: "no cascade was
    detected" is an outcome with no waves in it, and making the caller
    distinguish that from ``None`` is what let both front ends collapse the whole
    thing into ``if result is not None: result = cascade_result``.

    ``max_waves`` bounds a pathological loop where each fix-up wave produces
    another batch of defects. Reaching it is not an error the loop can resolve —
    but it is not a success either, so it is *reported* rather than hidden behind
    the last wave's result.

    Whatever ends the loop, one final detection pass records what is still
    outstanding. That is the difference between "we are done" and "we stopped".
    """
    waves: list[SessionResult] = []
    declined = False
    limit_reached = True
    for _wave in range(max_waves):
        tasks = session.detect_cascade_tasks()
        if not tasks:
            limit_reached = False
            break
        if announce is not None:
            announce(tasks)
        approved = approve(tasks)
        if approved is None:
            declined = True
            limit_reached = False
            break
        session.install_plan(approved)
        waves.append(session.run())

    unresolved = tuple(task.task_id for task in session.detect_cascade_tasks())
    return CascadeOutcome(
        waves=tuple(waves),
        declined=declined,
        limit_reached=limit_reached,
        unresolved=unresolved,
    )
