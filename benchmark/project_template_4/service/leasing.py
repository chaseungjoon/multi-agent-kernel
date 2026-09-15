"""Leasing service operations awaiting implementation."""

from __future__ import annotations

from dataclasses import replace

from service.models import Job, Submission


def claim(job: Job, worker: str, now: int, ttl: int) -> Job:
    """Claim queued job only when due <= now. Require nonempty worker, now >=0, ttl >0 or ValueError. Invalid state/not yet due also ValueError. Return replaced Job: state running, attempts +1, owner worker, lease_until now+ttl, updated now; preserve other fields."""
    raise NotImplementedError


def heartbeat(job: Job, worker: str, now: int, ttl: int) -> Job:
    """Renew only running job owned by nonempty worker with lease_until > now. Require now >= updated and now >=0 and ttl >0; otherwise ValueError. Return copy with lease_until=max(old lease_until, now+ttl) and updated=now, preserving other fields."""
    raise NotImplementedError


def expired_leases(jobs: tuple[Job, ...], now: int) -> tuple[str, ...]:
    """Return lexically sorted IDs of running jobs whose lease_until <= now. Ignore all other states. Negative now raises ValueError."""
    raise NotImplementedError


def requeue_expired(job: Job, now: int) -> Job:
    """Require now >=0. If running with lease_until <= now, return copy queued, owner empty, lease_until=0, due=now, updated=now; preserve attempts and other fields. Otherwise return unchanged job."""
    raise NotImplementedError
