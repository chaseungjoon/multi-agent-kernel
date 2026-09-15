"""Scheduling service operations awaiting implementation."""

from __future__ import annotations

from dataclasses import replace

from service.models import Job, Submission


def retry_delay(attempt: int, base: int, cap: int) -> int:
    """Return min(cap, base * 2**(attempt-1)); attempt >=1, base >=1, cap >=base required or ValueError. Handle arbitrarily large attempt without building enormous integers."""
    raise NotImplementedError


def ready_jobs(jobs: tuple[Job, ...], now: int, limit: int) -> tuple[Job, ...]:
    """Return queued jobs with due <= now, sorted by (due, id), capped at limit. Negative now or limit raises ValueError. Zero limit returns empty."""
    raise NotImplementedError


def retry_after(value: str, now: int) -> int | None:
    """Parse Retry-After as ASCII digits (delay seconds) or RFC HTTP date with timezone using email.utils.parsedate_to_datetime. Strip outer whitespace. Return nonnegative delay, rounding a date difference up. Invalid/naive date, signed numeric text, empty input returns None. Negative now raises ValueError."""
    raise NotImplementedError


def retryable_status(status: int) -> bool:
    """True only for HTTP 408, 425, 429 and 500..599; all other integers False."""
    raise NotImplementedError
