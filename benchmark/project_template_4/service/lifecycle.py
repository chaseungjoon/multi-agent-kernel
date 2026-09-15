"""Lifecycle service operations awaiting implementation."""

from __future__ import annotations

from dataclasses import replace

from service.models import Job, Submission


def complete(job: Job, worker: str, now: int) -> Job:
    """Require running state, nonempty matching worker, unexpired lease (lease_until > now), now >=0 and now >= updated, else ValueError. Return copy succeeded, owner empty, lease_until=0, updated=now, preserving all other fields."""
    raise NotImplementedError


def fail(job: Job, worker: str, now: int, max_attempts: int, delay: int) -> Job:
    """Require running job, nonempty matching worker, lease_until > now, now >= max(0, updated), max_attempts>=1, delay>=0 else ValueError. If attempts >= max_attempts return dead copy with due unchanged; otherwise queued with due=now+delay. Both clear owner/lease and set updated=now; preserve attempts."""
    raise NotImplementedError


def cancel(job: Job, tenant: str, now: int) -> Job:
    """Require nonempty matching tenant and now >= max(0, updated), otherwise ValueError. Already cancelled returns unchanged; only queued/running can become cancelled, other states ValueError. Clear owner/lease and set updated=now; preserve other fields."""
    raise NotImplementedError


def replay(job: Job, tenant: str, now: int) -> Job:
    """Only dead jobs of a nonempty matching tenant can replay; require now >= max(0, updated), else ValueError. Return queued copy, attempts=0, owner empty, lease_until=0, due=updated=now; preserve id, tenant, key, payload."""
    raise NotImplementedError
