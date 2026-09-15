"""Operations service operations awaiting implementation."""

from __future__ import annotations

from dataclasses import replace

from service.models import Job, Submission


def page_jobs(jobs: tuple[Job, ...], tenant: str, after: str, limit: int) -> tuple[Job, ...]:
    """Require 1<=limit<=100 or ValueError. Return tenant jobs with id lexically greater than exclusive cursor after, sorted by id, at most limit. Empty tenant returns empty."""
    raise NotImplementedError


def state_counts(jobs: tuple[Job, ...], tenant: str) -> tuple[tuple[str, int], ...]:
    """Return counts for this tenant in fixed state order queued, running, succeeded, dead, cancelled, including zero counts. Ignore unknown states and other tenants. Empty tenant produces all zeros."""
    raise NotImplementedError


def purge_candidates(jobs: tuple[Job, ...], tenant: str, before: int) -> tuple[str, ...]:
    """Return sorted IDs of tenant jobs in succeeded/cancelled with updated strictly before cutoff. Preserve dead jobs for replay and active jobs. Empty tenant returns empty; negative cutoff raises ValueError."""
    raise NotImplementedError


def oldest_ready_age(jobs: tuple[Job, ...], tenant: str, now: int) -> int:
    """Return maximum now-due among tenant queued jobs due <= now, or zero if none. Empty tenant returns zero. Negative now raises ValueError."""
    raise NotImplementedError
