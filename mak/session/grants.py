"""The lock set each dispatched task holds, and how its locks are given back."""

from __future__ import annotations

from mak.core.types import LockMode, NodeId, SubTask
from mak.scheduler.lock_policy import lock_requests
from mak.session.types import LockTableLike
from mak.session.wave import WaveState


def record_grant(wave: WaveState, task: SubTask) -> None:
    """Remember the lock set a task was dispatched under."""
    wave.granted[task.task_id] = dict(lock_requests(task, wave.lock_policy))


def granted_mode(wave: WaveState, task_id: str, node_id: NodeId) -> LockMode:
    """Return the mode ``task_id`` was granted on a target (WRITE if unknown)."""
    return wave.granted.get(task_id, {}).get(node_id, LockMode.WRITE)


def release_lock(
    lock_table: LockTableLike, wave: WaveState, task_id: str, node_id: NodeId
) -> None:
    """Give back the lock ``task_id`` holds on ``node_id``, in the mode it holds."""
    lock_table.release(node_id, granted_mode(wave, task_id, node_id), task_id)
