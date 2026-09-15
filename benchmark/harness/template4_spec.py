# Literal contracts and oracle source retain readable, complete expressions.
# ruff: noqa: E501
"""Executable contracts for a multi-tenant background-job service benchmark.

References are available only to the mock backend. Cases contain independently
specified expected values; neither the planner nor real workers receive them.
"""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent

MODELS = '''"""Immutable service records; time is injected as integer epoch seconds."""

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
'''

CONTEXT = (
    "You are implementing an existing multi-tenant background-job service. "
    "Use only the Python standard library. All timestamps are integer seconds; "
    "no wall clock or I/O. Inputs must not be mutated. Job and Submission are "
    "already imported from service.models; replace is imported from dataclasses. "
    "Return exactly the requested function, including any additional imports INSIDE "
    "it. Do not implement other functions or depend on unimplemented helpers.\n\n"
    + MODELS
)

SHARED_TABLES = ("routes", "events", "policies")


@dataclass(frozen=True)
class Case:
    """One independently scored contract or regression check."""

    name: str
    body: str


@dataclass(frozen=True)
class Task:
    """One independently implementable function with shared integration points."""

    module: str
    name: str
    signature: str
    contract: str
    body: str
    checks: tuple[str, ...]
    tables: tuple[str, ...] = ()

    @property
    def reference(self) -> str:
        """Return the mock implementation with its public contract."""
        return self.stub.replace("    raise NotImplementedError\n", self.body)

    @property
    def stub(self) -> str:
        """Return the exact contract given to real workers."""
        return f'def {self.name}{self.signature}:\n    """{self.contract}"""\n    raise NotImplementedError\n'

    @property
    def cases(self) -> tuple[Case, ...]:
        """Return individually named assertions for the generated oracle."""
        return tuple(
            Case(f"{self.name}_{i}", check) for i, check in enumerate(self.checks)
        )


def _body(source: str) -> str:
    return (
        "\n".join("    " + line for line in dedent(source).strip().splitlines()) + "\n"
    )


def _task(
    module: str,
    name: str,
    signature: str,
    contract: str,
    source: str,
    checks: tuple[str, ...],
    tables: tuple[str, ...] = (),
) -> Task:
    return Task(module, name, signature, contract, _body(source), checks, tables)


TASKS = (
    _task(
        "tenancy",
        "authorize",
        "(actor_tenant: str, resource_tenant: str, role: str, action: str) -> bool",
        "Allow only equal nonempty tenant IDs. reader can read; operator can read, submit, cancel; admin can also replay and purge. Unknown roles/actions deny.",
        """
          roles = {"reader": {"read"}, "operator": {"read", "submit", "cancel"},
                   "admin": {"read", "submit", "cancel", "replay", "purge"}}
          return bool(actor_tenant) and actor_tenant == resource_tenant and action in roles.get(role, set())
          """,
        (
            'assert tenancy.authorize("acme", "acme", "reader", "read")',
            'assert not tenancy.authorize("acme", "other", "admin", "purge")',
            'assert not tenancy.authorize("", "", "admin", "read")',
            'assert not tenancy.authorize("acme", "acme", "reader", "cancel")',
            'assert tenancy.authorize("acme", "acme", "operator", "submit")',
            'assert not tenancy.authorize("acme", "acme", "admin", "unknown")',
            'assert not tenancy.authorize("acme", "acme", "unknown", "read")',
        ),
        ("routes", "policies"),
    ),
    _task(
        "tenancy",
        "visible_jobs",
        "(jobs: tuple[Job, ...], tenant: str) -> tuple[Job, ...]",
        "Return jobs belonging to the nonempty tenant, in input order. Empty tenant returns empty. Do not mutate input.",
        "return tuple(job for job in jobs if tenant and job.tenant == tenant)",
        (
            'assert tenancy.visible_jobs((Job("a"), Job("b", tenant="other"), Job("c")), "acme") == (Job("a"), Job("c"))',
            'assert tenancy.visible_jobs((Job("x", tenant=""),), "") == ()',
            'assert tenancy.visible_jobs((), "acme") == ()',
            'jobs = (Job("a"),)\nassert tenancy.visible_jobs(jobs, "other") == ()\nassert jobs == (Job("a"),)',
        ),
        ("routes",),
    ),
    _task(
        "tenancy",
        "quota_remaining",
        "(jobs: tuple[Job, ...], tenant: str, limit: int) -> int",
        "Return max(0, limit minus tenant jobs in queued or running states). Other tenants and terminal states do not count. Negative limit raises ValueError.",
        """
          if limit < 0:
              raise ValueError("limit must be nonnegative")
          return max(0, limit - sum(j.tenant == tenant and j.state in {"queued", "running"} for j in jobs))
          """,
        (
            'assert tenancy.quota_remaining((Job("a"), Job("b", state="running"), Job("c", state="dead"), Job("d", tenant="other")), "acme", 5) == 3',
            'assert tenancy.quota_remaining((Job("a"),), "acme", 0) == 0',
            'assert tenancy.quota_remaining((), "acme", 3) == 3',
            'with pytest.raises(ValueError):\n    tenancy.quota_remaining((), "acme", -1)',
        ),
        ("policies",),
    ),
    _task(
        "tenancy",
        "validate_tenant",
        "(tenant: str) -> str",
        "Strip whitespace and lowercase. Require 3..32 ASCII characters, first character a-z and all remaining characters a-z, 0-9 or hyphen; otherwise raise ValueError.",
        """
          import re
          result = tenant.strip().lower()
          if not re.fullmatch(r"[a-z][a-z0-9-]{2,31}", result):
              raise ValueError("invalid tenant")
          return result
          """,
        (
            'assert tenancy.validate_tenant("  Acme-42 ") == "acme-42"',
            'assert tenancy.validate_tenant("a" * 32) == "a" * 32',
            'with pytest.raises(ValueError):\n    tenancy.validate_tenant("a" * 33)',
            'with pytest.raises(ValueError):\n    tenancy.validate_tenant("équipe")',
            'with pytest.raises(ValueError):\n    tenancy.validate_tenant("1ab")',
            'with pytest.raises(ValueError):\n    tenancy.validate_tenant("ab")',
        ),
    ),
    _task(
        "submission",
        "canonical_payload",
        "(payload: str) -> str",
        "Parse JSON and require an object. Return compact JSON with sorted keys, ensure_ascii=False, allowing only finite numbers (including nested values). Invalid JSON or non-object/nonfinite values raise ValueError.",
        """
          import json
          value = json.loads(payload)
          if not isinstance(value, dict):
              raise ValueError("payload must be an object")
          return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
          """,
        (
            'assert submission.canonical_payload(\'{"z": 1, "a": {"y": 2, "b": "한"}}\') == \'{"a":{"b":"한","y":2},"z":1}\'',
            'assert submission.canonical_payload("{}") == "{}"',
            'with pytest.raises(ValueError):\n    submission.canonical_payload("[]")',
            'with pytest.raises(ValueError):\n    submission.canonical_payload("broken")',
            "with pytest.raises(ValueError):\n    submission.canonical_payload('{\"x\": NaN}')",
            "with pytest.raises(ValueError):\n    submission.canonical_payload('{\"x\": [1e999]}')",
        ),
    ),
    _task(
        "submission",
        "find_duplicate",
        "(jobs: tuple[Job, ...], request: Submission) -> Job | None",
        "For nonempty request.key find same tenant/key regardless of state; return matching Job if payload strings equal, raise ValueError if any matching job has differing payload. If several match return smallest id lexically. Empty key or no match returns None. Compare payloads literally.",
        """
          matches = [j for j in jobs if request.key and j.tenant == request.tenant and j.key == request.key]
          if any(j.payload != request.payload for j in matches):
              raise ValueError("idempotency key reused with different payload")
          return min(matches, key=lambda j: j.id) if matches else None
          """,
        (
            'assert submission.find_duplicate((Job("a", key="k", state="succeeded"),), Submission("b", "acme", "k", "{}", 10)) == Job("a", key="k", state="succeeded")',
            'assert submission.find_duplicate((Job("a", tenant="other", key="k"),), Submission("b", "acme", "k", "{}", 10)) is None',
            'assert submission.find_duplicate((Job("a"),), Submission("b", "acme", "", "{}", 10)) is None',
            'with pytest.raises(ValueError):\n    submission.find_duplicate((Job("a", key="k", payload="x"),), Submission("b", "acme", "k", "{}", 10))',
            'assert submission.find_duplicate((Job("z", key="k"), Job("a", key="k")), Submission("b", "acme", "k", "{}", 0)).id == "a"',
        ),
        ("routes", "policies"),
    ),
    _task(
        "submission",
        "create_job",
        "(request: Submission) -> Job",
        "Require nonempty id and tenant, now >= 0; otherwise ValueError. Create queued Job copying id, tenant, key, payload literally; due and updated equal now, all other fields use defaults.",
        """
          if not request.id or not request.tenant or request.now < 0:
              raise ValueError("id, tenant and nonnegative timestamp required")
          return Job(request.id, request.tenant, request.key, request.payload, due=request.now, updated=request.now)
          """,
        (
            'assert submission.create_job(Submission("a", "other", "k", "body", 12)) == Job("a", "other", "k", "body", due=12, updated=12)',
            'assert submission.create_job(Submission("a", "acme", "", "{}", 0)) == Job("a")',
            'with pytest.raises(ValueError):\n    submission.create_job(Submission("", "acme", "", "{}", 0))',
            'with pytest.raises(ValueError):\n    submission.create_job(Submission("a", "", "", "{}", 0))',
            'with pytest.raises(ValueError):\n    submission.create_job(Submission("a", "acme", "", "{}", -1))',
        ),
        ("routes", "events"),
    ),
    _task(
        "submission",
        "payload_digest",
        "(payload: str) -> str",
        "Return lowercase SHA-256 hexadecimal digest of the exact UTF-8 payload bytes; no JSON normalization.",
        """
          import hashlib
          return hashlib.sha256(payload.encode("utf-8")).hexdigest()
          """,
        (
            'assert submission.payload_digest("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"',
            'assert submission.payload_digest("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"',
            'assert submission.payload_digest("{}") != submission.payload_digest("{ }")',
        ),
    ),
    _task(
        "scheduling",
        "retry_delay",
        "(attempt: int, base: int, cap: int) -> int",
        "Return min(cap, base * 2**(attempt-1)); attempt >=1, base >=1, cap >=base required or ValueError. Handle arbitrarily large attempt without building enormous integers.",
        """
          if attempt < 1 or base < 1 or cap < base:
              raise ValueError("invalid retry policy")
          if attempt - 1 >= cap.bit_length():
              return cap
          return min(cap, base << (attempt - 1))
          """,
        (
            "assert scheduling.retry_delay(1, 5, 60) == 5",
            "assert scheduling.retry_delay(4, 5, 60) == 40",
            "assert scheduling.retry_delay(5, 5, 60) == 60",
            "assert scheduling.retry_delay(10**12, 5, 60) == 60",
            "with pytest.raises(ValueError):\n    scheduling.retry_delay(0, 5, 60)",
            "with pytest.raises(ValueError):\n    scheduling.retry_delay(1, 0, 60)",
            "with pytest.raises(ValueError):\n    scheduling.retry_delay(1, 5, 4)",
        ),
        ("policies",),
    ),
    _task(
        "scheduling",
        "ready_jobs",
        "(jobs: tuple[Job, ...], now: int, limit: int) -> tuple[Job, ...]",
        "Return queued jobs with due <= now, sorted by (due, id), capped at limit. Negative now or limit raises ValueError. Zero limit returns empty.",
        """
          if now < 0 or limit < 0:
              raise ValueError("nonnegative now and limit required")
          return tuple(sorted((j for j in jobs if j.state == "queued" and j.due <= now), key=lambda j: (j.due, j.id))[:limit])
          """,
        (
            'assert scheduling.ready_jobs((Job("b", due=2), Job("a", due=2), Job("c", due=1)), 2, 2) == (Job("c", due=1), Job("a", due=2))',
            'assert scheduling.ready_jobs((Job("a", due=3), Job("b", state="running")), 2, 10) == ()',
            'assert scheduling.ready_jobs((Job("a"),), 0, 0) == ()',
            "with pytest.raises(ValueError):\n    scheduling.ready_jobs((), 0, -1)",
            "with pytest.raises(ValueError):\n    scheduling.ready_jobs((), -1, 1)",
        ),
        ("routes", "events"),
    ),
    _task(
        "scheduling",
        "retry_after",
        "(value: str, now: int) -> int | None",
        "Parse Retry-After as ASCII digits (delay seconds) or RFC HTTP date with timezone using email.utils.parsedate_to_datetime. Strip outer whitespace. Return nonnegative delay, rounding a date difference up. Invalid/naive date, signed numeric text, empty input returns None. Negative now raises ValueError.",
        """
          import math
          from email.utils import parsedate_to_datetime
          if now < 0:
              raise ValueError("now must be nonnegative")
          value = value.strip()
          if value.isascii() and value.isdigit():
              return int(value)
          try:
              date = parsedate_to_datetime(value)
          except (ValueError, TypeError, OverflowError):
              return None
          if date.tzinfo is None:
              return None
          return max(0, math.ceil(date.timestamp() - now))
          """,
        (
            'assert scheduling.retry_after(" 12 ", 0) == 12',
            'assert scheduling.retry_after("Thu, 01 Jan 1970 00:01:00 GMT", 10) == 50',
            'assert scheduling.retry_after("Thu, 01 Jan 1970 00:00:00 GMT", 10) == 0',
            'assert scheduling.retry_after("-2", 0) is None',
            'assert scheduling.retry_after("", 0) is None',
            'assert scheduling.retry_after("Thu, 01 Jan 1970 00:01:00", 0) is None',
            'with pytest.raises(ValueError):\n    scheduling.retry_after("1", -1)',
        ),
    ),
    _task(
        "scheduling",
        "retryable_status",
        "(status: int) -> bool",
        "True only for HTTP 408, 425, 429 and 500..599; all other integers False.",
        "return status in {408, 425, 429} or 500 <= status <= 599",
        (
            "assert all(scheduling.retryable_status(s) for s in (408, 425, 429, 500, 503, 599))",
            "assert not any(scheduling.retryable_status(s) for s in (-1, 200, 400, 401, 404, 499, 600))",
        ),
        ("policies",),
    ),
    _task(
        "leasing",
        "claim",
        "(job: Job, worker: str, now: int, ttl: int) -> Job",
        "Claim queued job only when due <= now. Require nonempty worker, now >=0, ttl >0 or ValueError. Invalid state/not yet due also ValueError. Return replaced Job: state running, attempts +1, owner worker, lease_until now+ttl, updated now; preserve other fields.",
        """
          if not worker or now < 0 or ttl <= 0 or job.state != "queued" or job.due > now:
              raise ValueError("job cannot be claimed")
          return replace(job, state="running", attempts=job.attempts + 1, owner=worker, lease_until=now + ttl, updated=now)
          """,
        (
            'assert leasing.claim(Job("a", due=10, attempts=2), "w", 10, 5) == Job("a", state="running", due=10, attempts=3, owner="w", lease_until=15, updated=10)',
            'with pytest.raises(ValueError):\n    leasing.claim(Job("a", due=11), "w", 10, 5)',
            'with pytest.raises(ValueError):\n    leasing.claim(Job("a", state="running"), "w", 0, 5)',
            'with pytest.raises(ValueError):\n    leasing.claim(Job("a"), "", 0, 5)',
            'with pytest.raises(ValueError):\n    leasing.claim(Job("a"), "w", 0, 0)',
            'job = Job("a")\nleasing.claim(job, "w", 0, 5)\nassert job == Job("a")',
        ),
        ("routes", "events"),
    ),
    _task(
        "leasing",
        "heartbeat",
        "(job: Job, worker: str, now: int, ttl: int) -> Job",
        "Renew only running job owned by nonempty worker with lease_until > now. Require now >= updated and now >=0 and ttl >0; otherwise ValueError. Return copy with lease_until=max(old lease_until, now+ttl) and updated=now, preserving other fields.",
        """
          if job.state != "running" or not worker or job.owner != worker or job.lease_until <= now or now < max(0, job.updated) or ttl <= 0:
              raise ValueError("lease cannot be renewed")
          return replace(job, lease_until=max(job.lease_until, now + ttl), updated=now)
          """,
        (
            'assert leasing.heartbeat(Job("a", state="running", owner="w", lease_until=10), "w", 5, 20).lease_until == 25',
            'assert leasing.heartbeat(Job("a", state="running", owner="w", lease_until=100), "w", 5, 1).lease_until == 100',
            'with pytest.raises(ValueError):\n    leasing.heartbeat(Job("a", state="running", owner="w", lease_until=10), "w", 10, 5)',
            'with pytest.raises(ValueError):\n    leasing.heartbeat(Job("a", state="running", owner="w", lease_until=10), "old", 5, 5)',
            'with pytest.raises(ValueError):\n    leasing.heartbeat(Job("a", state="running", owner="w", updated=6, lease_until=10), "w", 5, 5)',
        ),
        ("routes",),
    ),
    _task(
        "leasing",
        "expired_leases",
        "(jobs: tuple[Job, ...], now: int) -> tuple[str, ...]",
        "Return lexically sorted IDs of running jobs whose lease_until <= now. Ignore all other states. Negative now raises ValueError.",
        """
          if now < 0:
              raise ValueError("now must be nonnegative")
          return tuple(sorted(j.id for j in jobs if j.state == "running" and j.lease_until <= now))
          """,
        (
            'assert leasing.expired_leases((Job("b", state="running", lease_until=10), Job("a", state="running", lease_until=9), Job("c", state="running", lease_until=11), Job("d")), 10) == ("a", "b")',
            "assert leasing.expired_leases((), 0) == ()",
            "with pytest.raises(ValueError):\n    leasing.expired_leases((), -1)",
        ),
        ("events", "policies"),
    ),
    _task(
        "leasing",
        "requeue_expired",
        "(job: Job, now: int) -> Job",
        "Require now >=0. If running with lease_until <= now, return copy queued, owner empty, lease_until=0, due=now, updated=now; preserve attempts and other fields. Otherwise return unchanged job.",
        """
          if now < 0:
              raise ValueError("now must be nonnegative")
          if job.state == "running" and job.lease_until <= now:
              return replace(job, state="queued", owner="", lease_until=0, due=now, updated=now)
          return job
          """,
        (
            'assert leasing.requeue_expired(Job("a", state="running", owner="w", attempts=2, lease_until=10), 10) == Job("a", attempts=2, due=10, updated=10)',
            'job = Job("a", state="running", owner="w", lease_until=11)\nassert leasing.requeue_expired(job, 10) == job',
            'job = Job("a", state="succeeded")\nassert leasing.requeue_expired(job, 10) == job',
            'with pytest.raises(ValueError):\n    leasing.requeue_expired(Job("a"), -1)',
        ),
        ("events",),
    ),
    _task(
        "lifecycle",
        "complete",
        "(job: Job, worker: str, now: int) -> Job",
        "Require running state, nonempty matching worker, unexpired lease (lease_until > now), now >=0 and now >= updated, else ValueError. Return copy succeeded, owner empty, lease_until=0, updated=now, preserving all other fields.",
        """
          if job.state != "running" or not worker or job.owner != worker or job.lease_until <= now or now < max(0, job.updated):
              raise ValueError("completion requires an active owned lease")
          return replace(job, state="succeeded", owner="", lease_until=0, updated=now)
          """,
        (
            'assert lifecycle.complete(Job("a", state="running", owner="w", lease_until=20, attempts=1), "w", 10) == Job("a", state="succeeded", attempts=1, updated=10)',
            'with pytest.raises(ValueError):\n    lifecycle.complete(Job("a", state="running", owner="w", lease_until=10), "w", 10)',
            'with pytest.raises(ValueError):\n    lifecycle.complete(Job("a", state="running", owner="w", lease_until=20), "old", 10)',
            'with pytest.raises(ValueError):\n    lifecycle.complete(Job("a", state="succeeded"), "w", 0)',
        ),
        ("routes", "events"),
    ),
    _task(
        "lifecycle",
        "fail",
        "(job: Job, worker: str, now: int, max_attempts: int, delay: int) -> Job",
        "Require running job, nonempty matching worker, lease_until > now, now >= max(0, updated), max_attempts>=1, delay>=0 else ValueError. If attempts >= max_attempts return dead copy with due unchanged; otherwise queued with due=now+delay. Both clear owner/lease and set updated=now; preserve attempts.",
        """
          if job.state != "running" or not worker or job.owner != worker or job.lease_until <= now or now < max(0, job.updated) or max_attempts < 1 or delay < 0:
              raise ValueError("failure requires an active lease and valid retry policy")
          dead = job.attempts >= max_attempts
          return replace(job, state="dead" if dead else "queued", due=job.due if dead else now + delay, owner="", lease_until=0, updated=now)
          """,
        (
            'assert lifecycle.fail(Job("a", state="running", owner="w", lease_until=20, attempts=1), "w", 10, 3, 5) == Job("a", attempts=1, due=15, updated=10)',
            'assert lifecycle.fail(Job("a", state="running", owner="w", lease_until=20, attempts=3, due=2), "w", 10, 3, 5) == Job("a", state="dead", attempts=3, due=2, updated=10)',
            'with pytest.raises(ValueError):\n    lifecycle.fail(Job("a", state="running", owner="w", lease_until=10), "w", 10, 3, 5)',
            'with pytest.raises(ValueError):\n    lifecycle.fail(Job("a", state="running", owner="w", lease_until=20), "w", 10, 0, 5)',
            'with pytest.raises(ValueError):\n    lifecycle.fail(Job("a", state="running", owner="w", lease_until=20), "w", 10, 3, -1)',
        ),
        ("events", "policies"),
    ),
    _task(
        "lifecycle",
        "cancel",
        "(job: Job, tenant: str, now: int) -> Job",
        "Require nonempty matching tenant and now >= max(0, updated), otherwise ValueError. Already cancelled returns unchanged; only queued/running can become cancelled, other states ValueError. Clear owner/lease and set updated=now; preserve other fields.",
        """
          if not tenant or tenant != job.tenant or now < max(0, job.updated):
              raise ValueError("invalid tenant or timestamp")
          if job.state == "cancelled":
              return job
          if job.state not in {"queued", "running"}:
              raise ValueError("terminal job cannot be cancelled")
          return replace(job, state="cancelled", owner="", lease_until=0, updated=now)
          """,
        (
            'assert lifecycle.cancel(Job("a", state="running", owner="w", lease_until=20), "acme", 10) == Job("a", state="cancelled", updated=10)',
            'job = Job("a", state="cancelled", updated=1)\nassert lifecycle.cancel(job, "acme", 10) == job',
            'with pytest.raises(ValueError):\n    lifecycle.cancel(Job("a"), "other", 10)',
            'with pytest.raises(ValueError):\n    lifecycle.cancel(Job("a", state="succeeded"), "acme", 10)',
            'with pytest.raises(ValueError):\n    lifecycle.cancel(Job("a", updated=11), "acme", 10)',
        ),
        ("routes",),
    ),
    _task(
        "lifecycle",
        "replay",
        "(job: Job, tenant: str, now: int) -> Job",
        "Only dead jobs of a nonempty matching tenant can replay; require now >= max(0, updated), else ValueError. Return queued copy, attempts=0, owner empty, lease_until=0, due=updated=now; preserve id, tenant, key, payload.",
        """
          if job.state != "dead" or not tenant or job.tenant != tenant or now < max(0, job.updated):
              raise ValueError("only tenant-owned dead jobs may replay")
          return replace(job, state="queued", attempts=0, owner="", lease_until=0, due=now, updated=now)
          """,
        (
            'assert lifecycle.replay(Job("a", key="k", state="dead", attempts=3), "acme", 10) == Job("a", key="k", due=10, updated=10)',
            'with pytest.raises(ValueError):\n    lifecycle.replay(Job("a", state="dead"), "other", 10)',
            'with pytest.raises(ValueError):\n    lifecycle.replay(Job("a", state="succeeded"), "acme", 10)',
            'with pytest.raises(ValueError):\n    lifecycle.replay(Job("a", state="dead", updated=11), "acme", 10)',
        ),
        ("routes", "events"),
    ),
    _task(
        "operations",
        "page_jobs",
        "(jobs: tuple[Job, ...], tenant: str, after: str, limit: int) -> tuple[Job, ...]",
        "Require 1<=limit<=100 or ValueError. Return tenant jobs with id lexically greater than exclusive cursor after, sorted by id, at most limit. Empty tenant returns empty.",
        """
          if not 1 <= limit <= 100:
              raise ValueError("page size must be 1..100")
          return tuple(sorted((j for j in jobs if tenant and j.tenant == tenant and j.id > after), key=lambda j: j.id)[:limit])
          """,
        (
            'assert operations.page_jobs((Job("c"), Job("b", tenant="other"), Job("a"), Job("d")), "acme", "a", 1) == (Job("c"),)',
            'assert operations.page_jobs((Job("a"),), "acme", "a", 100) == ()',
            'assert operations.page_jobs((Job("a", tenant=""),), "", "", 1) == ()',
            'with pytest.raises(ValueError):\n    operations.page_jobs((), "acme", "", 0)',
            'with pytest.raises(ValueError):\n    operations.page_jobs((), "acme", "", 101)',
        ),
        ("routes",),
    ),
    _task(
        "operations",
        "state_counts",
        "(jobs: tuple[Job, ...], tenant: str) -> tuple[tuple[str, int], ...]",
        "Return counts for this tenant in fixed state order queued, running, succeeded, dead, cancelled, including zero counts. Ignore unknown states and other tenants. Empty tenant produces all zeros.",
        """
          states = ("queued", "running", "succeeded", "dead", "cancelled")
          return tuple((state, sum(bool(tenant) and j.tenant == tenant and j.state == state for j in jobs)) for state in states)
          """,
        (
            'assert operations.state_counts((Job("a"), Job("b"), Job("c", state="dead"), Job("d", tenant="other"), Job("e", state="unknown")), "acme") == (("queued", 2), ("running", 0), ("succeeded", 0), ("dead", 1), ("cancelled", 0))',
            'assert operations.state_counts((), "acme") == (("queued", 0), ("running", 0), ("succeeded", 0), ("dead", 0), ("cancelled", 0))',
            'assert operations.state_counts((Job("a", tenant=""),), "")[0] == ("queued", 0)',
        ),
        ("routes", "events"),
    ),
    _task(
        "operations",
        "purge_candidates",
        "(jobs: tuple[Job, ...], tenant: str, before: int) -> tuple[str, ...]",
        "Return sorted IDs of tenant jobs in succeeded/cancelled with updated strictly before cutoff. Preserve dead jobs for replay and active jobs. Empty tenant returns empty; negative cutoff raises ValueError.",
        """
          if before < 0:
              raise ValueError("cutoff must be nonnegative")
          return tuple(sorted(j.id for j in jobs if tenant and j.tenant == tenant and j.state in {"succeeded", "cancelled"} and j.updated < before))
          """,
        (
            'assert operations.purge_candidates((Job("z", state="cancelled", updated=1), Job("a", state="succeeded", updated=2), Job("b", state="succeeded", updated=3), Job("c", state="dead"), Job("d"), Job("e", tenant="other", state="succeeded")), "acme", 3) == ("a", "z")',
            'assert operations.purge_candidates((Job("a", state="succeeded"),), "acme", 0) == ()',
            'with pytest.raises(ValueError):\n    operations.purge_candidates((), "acme", -1)',
        ),
        ("events", "policies"),
    ),
    _task(
        "operations",
        "oldest_ready_age",
        "(jobs: tuple[Job, ...], tenant: str, now: int) -> int",
        "Return maximum now-due among tenant queued jobs due <= now, or zero if none. Empty tenant returns zero. Negative now raises ValueError.",
        """
          if now < 0:
              raise ValueError("now must be nonnegative")
          return max((now - j.due for j in jobs if tenant and j.tenant == tenant and j.state == "queued" and j.due <= now), default=0)
          """,
        (
            'assert operations.oldest_ready_age((Job("a", due=2), Job("b", due=5), Job("c", tenant="other", due=0), Job("d", state="running", due=0)), "acme", 10) == 8',
            'assert operations.oldest_ready_age((Job("a", due=11),), "acme", 10) == 0',
            'assert operations.oldest_ready_age((), "acme", 10) == 0',
            'with pytest.raises(ValueError):\n    operations.oldest_ready_age((), "acme", -1)',
        ),
    ),
)


def modules() -> tuple[str, ...]:
    """Return the ordered ownership units for planning."""
    return tuple(dict.fromkeys(task.module for task in TASKS))


def expected_tests() -> int:
    """Return the fixed denominator, including wiring and lifecycle workflows."""
    from harness.template4_workflows import WORKFLOWS

    return sum(len(task.checks) + len(task.tables) for task in TASKS) + len(WORKFLOWS)
