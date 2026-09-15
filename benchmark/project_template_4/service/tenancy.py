"""Tenancy service operations awaiting implementation."""

from __future__ import annotations

from dataclasses import replace

from service.models import Job, Submission


def authorize(actor_tenant: str, resource_tenant: str, role: str, action: str) -> bool:
    """Allow only equal nonempty tenant IDs. reader can read; operator can read, submit, cancel; admin can also replay and purge. Unknown roles/actions deny."""
    raise NotImplementedError


def visible_jobs(jobs: tuple[Job, ...], tenant: str) -> tuple[Job, ...]:
    """Return jobs belonging to the nonempty tenant, in input order. Empty tenant returns empty. Do not mutate input."""
    raise NotImplementedError


def quota_remaining(jobs: tuple[Job, ...], tenant: str, limit: int) -> int:
    """Return max(0, limit minus tenant jobs in queued or running states). Other tenants and terminal states do not count. Negative limit raises ValueError."""
    raise NotImplementedError


def validate_tenant(tenant: str) -> str:
    """Strip whitespace and lowercase. Require 3..32 ASCII characters, first character a-z and all remaining characters a-z, 0-9 or hyphen; otherwise raise ValueError."""
    raise NotImplementedError
