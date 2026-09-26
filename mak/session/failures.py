"""The per-task failure log: why each attempt made no progress."""

from __future__ import annotations

from mak.session.types import SubTaskProgress
from mak.session.wave import WaveState


def record_failure(wave: WaveState, task_id: str, reason: str) -> None:
    """Record why an attempt made no progress, keeping the earlier ones.

    ``failure_reasons`` holds the latest reason (what the retry acts on);
    ``failure_history`` accumulates the distinct ones so the *final* report
    can name a cause that recurred across attempts rather than whichever
    happened to land last.
    """
    wave.failure_reasons[task_id] = reason
    history = wave.failure_history.setdefault(task_id, [])
    if reason not in history:
        history.append(reason)


def final_failure_reason(wave: WaveState, progress: SubTaskProgress) -> str:
    """Summarize why a task failed across *all* of its attempts.

    A task can fail differently each time, and reporting only the last
    attempt buries the cause: attempts rejected on one underlying defect,
    followed by a one-off malformed response, would report only the
    malformed response. Distinct reasons are therefore all reported, in the
    order they were first seen.
    """
    history = wave.failure_history.get(progress.task_id, [])
    if not history:
        return (
            "agent reported success but staged no usable source "
            f"after {progress.attempts} attempt(s)"
        )
    if len(history) == 1:
        return history[0]
    listed = "; ".join(f"({i}) {r}" for i, r in enumerate(history, 1))
    return (
        f"{progress.attempts} attempts failed for {len(history)} "
        f"reasons: {listed}"
    )
