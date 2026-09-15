"""Submission service operations awaiting implementation."""

from __future__ import annotations

from dataclasses import replace

from service.models import Job, Submission


def canonical_payload(payload: str) -> str:
    """Parse JSON and require an object. Return compact JSON with sorted keys, ensure_ascii=False, allowing only finite numbers (including nested values). Invalid JSON or non-object/nonfinite values raise ValueError."""
    raise NotImplementedError


def find_duplicate(jobs: tuple[Job, ...], request: Submission) -> Job | None:
    """For nonempty request.key find same tenant/key regardless of state; return matching Job if payload strings equal, raise ValueError if any matching job has differing payload. If several match return smallest id lexically. Empty key or no match returns None. Compare payloads literally."""
    raise NotImplementedError


def create_job(request: Submission) -> Job:
    """Require nonempty id and tenant, now >= 0; otherwise ValueError. Create queued Job copying id, tenant, key, payload literally; due and updated equal now, all other fields use defaults."""
    raise NotImplementedError


def payload_digest(payload: str) -> str:
    """Return lowercase SHA-256 hexadecimal digest of the exact UTF-8 payload bytes; no JSON normalization."""
    raise NotImplementedError
