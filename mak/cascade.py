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
import hashlib
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
    # The immediately previous broken repository state reappeared: the repair
    # made no semantic progress.
    stalled: bool = False
    # A non-adjacent prior state reappeared: A -> B -> A.
    oscillating: bool = False
    # No reviewed task retained a target capable of satisfying the generated
    # repair postcondition.
    unrepairable: bool = False
    # Human-readable deterministic evidence for a kernel-stopped loop.
    stop_reason: str | None = None
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
            and not self.stalled
            and not self.oscillating
            and not self.unrepairable
            and not self.unresolved
            and all(result.ok for result in self.waves)
        )


class _CascadingSession(Protocol):
    """The slice of ``Session`` this loop drives."""

    def detect_cascade_tasks(self) -> list[SubTask]: ...

    def install_plan(self, subtasks: list[SubTask]) -> None: ...

    def run(self, max_iterations: int = ...) -> SessionResult: ...


def _fallback_fingerprint(tasks: list[SubTask]) -> str:
    """Digest a repair batch when a test double has no repository state API."""
    digest = hashlib.blake2s(digest_size=16)
    for task in sorted(tasks, key=lambda item: item.task_id):
        digest.update(task.task_id.encode())
        digest.update(b"\0")
        for node in (*task.target_nodes, *task.context_nodes):
            digest.update(str(node).encode())
            digest.update(b"\0")
        for obligation in task.repair_obligations:
            digest.update(obligation.family_key.encode())
            digest.update(b"\0")
    return digest.hexdigest()


def _state_fingerprint(session: _CascadingSession, tasks: list[SubTask]) -> str:
    method = getattr(session, "cascade_state_fingerprint", None)
    if callable(method):
        return str(method(tasks))
    return _fallback_fingerprint(tasks)


def _persisted_history(session: _CascadingSession) -> list[str]:
    method = getattr(session, "cascade_history", None)
    if not callable(method):
        return []
    return [str(item) for item in method()]


def _remember_state(session: _CascadingSession, fingerprint: str) -> None:
    method = getattr(session, "remember_cascade_state", None)
    if callable(method):
        method(fingerprint)


def _depends_transitively(
    tasks: dict[str, SubTask], start: str, wanted: str
) -> bool:
    """Whether ``start`` already has ``wanted`` as a direct or indirect input."""
    pending = list(tasks[start].depends_on)
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == wanted:
            return True
        if current in seen or current not in tasks:
            continue
        seen.add(current)
        pending.extend(tasks[current].depends_on)
    return False


def _preserve_obligations(
    detected: list[SubTask], approved: list[SubTask]
) -> list[SubTask] | None:
    """Keep kernel postconditions when the reviewer edits a generated plan.

    Edited planner JSON cannot declare the kernel-only obligation field. Reattach
    every obligation to a downstream approved task that writes its caller or
    provider, and make that task depend on the other writers. Its prospective
    state can then truthfully discharge the postcondition even when review split
    one generated repair into several tasks.
    """
    obligations = [
        obligation for task in detected for obligation in task.repair_obligations
    ]
    result = list(approved)
    for obligation in obligations:
        caller = [
            index
            for index, task in enumerate(result)
            if any(
                str(node).split("::", 1)[0] == obligation.file
                for node in task.target_nodes
            )
        ]
        provider = [
            index
            for index, task in enumerate(result)
            if any(
                str(node).split("::", 1)[0] == obligation.defining_file
                for node in task.target_nodes
            )
        ]
        candidates = sorted(set([*caller, *provider]))
        if not candidates:
            return None
        by_id = {task.task_id: task for task in result}
        # Pick a candidate that is not already upstream of another candidate;
        # adding the remaining writers as dependencies cannot make a cycle.
        sinks = [
            index
            for index in candidates
            if not any(
                index != other
                and _depends_transitively(
                    by_id, result[other].task_id, result[index].task_id
                )
                for other in candidates
            )
        ]
        if not sinks:
            return None
        index = sinks[-1]
        required = [
            result[other].task_id
            for other in candidates
            if other != index
        ]
        existing = result[index].repair_obligations
        result[index] = dataclasses.replace(
            result[index],
            depends_on=list(dict.fromkeys([
                *result[index].depends_on,
                *required,
            ])),
            repair_obligations=(
                existing
                if obligation in existing
                else (*existing, obligation)
            ),
        )
    return result


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

    A clean result and the wave ceiling get one final confirmation pass. Other
    stop paths retain the batch that caused the stop. That is the difference
    between "we are done" and "we stopped".
    """
    waves: list[SessionResult] = []
    declined = False
    stalled = False
    oscillating = False
    unrepairable = False
    stop_reason: str | None = None
    limit_reached = True
    seen = _persisted_history(session)
    outstanding: list[SubTask] = []
    for _wave in range(max_waves):
        tasks = session.detect_cascade_tasks()
        if not tasks:
            limit_reached = False
            outstanding = session.detect_cascade_tasks()
            break
        fingerprint = _state_fingerprint(session, tasks)
        if fingerprint in seen:
            outstanding = tasks
            limit_reached = False
            if seen and fingerprint == seen[-1]:
                stalled = True
                stop_reason = (
                    "the same broken repository state remained after the last "
                    "repair; another identical approval would make no progress"
                )
            else:
                oscillating = True
                stop_reason = (
                    "a previously seen broken repository state reappeared; "
                    "the repairs are oscillating"
                )
            break
        if announce is not None:
            announce(tasks)
        approved = approve(tasks)
        if approved is None:
            declined = True
            limit_reached = False
            outstanding = tasks
            break
        approved = _preserve_obligations(tasks, approved)
        if approved is None or not approved:
            unrepairable = True
            limit_reached = False
            outstanding = tasks
            stop_reason = (
                "the approved cascade plan contains no task that can discharge "
                "the detected repair obligations"
            )
            break
        # A declined wave was never attempted and must not poison recovery.
        # Remember a state only once a concrete repair plan is about to run.
        seen.append(fingerprint)
        _remember_state(session, fingerprint)
        session.install_plan(approved)
        waves.append(session.run())
    else:
        outstanding = session.detect_cascade_tasks()
    unresolved = tuple(task.task_id for task in outstanding)
    return CascadeOutcome(
        waves=tuple(waves),
        declined=declined,
        limit_reached=limit_reached,
        stalled=stalled,
        oscillating=oscillating,
        unrepairable=unrepairable,
        stop_reason=stop_reason,
        unresolved=unresolved,
    )
