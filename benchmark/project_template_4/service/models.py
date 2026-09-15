"""Immutable service records; time is injected as integer epoch seconds."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    """A tenant-owned job and its current delivery state."""

    id: str
    tenant: str = "acme"
    key: str = ""
    payload: str = "{}"
    state: str = "queued"
    attempts: int = 0
    due: int = 0
    owner: str = ""
    lease_until: int = 0
    updated: int = 0


@dataclass(frozen=True)
class Submission:
    """An authenticated job submission with a client idempotency key."""

    id: str
    tenant: str
    key: str
    payload: str
    now: int
