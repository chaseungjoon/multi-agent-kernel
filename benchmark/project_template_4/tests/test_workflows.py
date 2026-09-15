"""Acceptance oracle with independently specified expected outcomes."""

from dataclasses import replace

import pytest

from service import (tenancy, submission, scheduling, leasing, lifecycle, operations, routes, events, policies)
from service.models import Job, Submission


def test_submit_deliver_observe() -> None:
    """Verify submit deliver observe."""
    payload = submission.canonical_payload('{"b":2,"a":1}')
    request = Submission("j1", tenancy.validate_tenant(" Acme "), "k", payload, 100)
    job = routes.lookup("create_job")(request)
    assert tenancy.authorize("acme", job.tenant, "operator", "submit")
    assert tenancy.quota_remaining((job,), "acme", 2) == 1
    ready = events.lookup("ready_jobs")((job,), 100, 1)
    running = routes.lookup("claim")(ready[0], "worker", 100, 30)
    renewed = leasing.heartbeat(running, "worker", 110, 30)
    finished = events.lookup("complete")(renewed, "worker", 120)
    assert finished == Job("j1", key="k", payload='{"a":1,"b":2}', state="succeeded", attempts=1, due=100, updated=120)
    assert operations.state_counts((finished,), "acme")[2] == ("succeeded", 1)
    assert submission.find_duplicate((finished,), request) == finished


def test_failure_retry_dead_letter_replay() -> None:
    """Verify failure retry dead letter replay."""
    job = submission.create_job(Submission("a", "acme", "k", "{}", 10))
    first = leasing.claim(job, "w1", 10, 20)
    retry = lifecycle.fail(first, "w1", 11, 2, policies.lookup("retry_delay")(1, 5, 30))
    assert scheduling.ready_jobs((retry,), 15, 1) == ()
    second = leasing.claim(scheduling.ready_jobs((retry,), 16, 1)[0], "w2", 16, 20)
    dead = lifecycle.fail(second, "w2", 17, 2, 10)
    assert dead.state == "dead" and dead.attempts == 2
    assert operations.purge_candidates((dead,), "acme", 1000) == ()
    replay = routes.lookup("replay")(dead, "acme", 20)
    assert replay == Job("a", key="k", due=20, updated=20)
    assert leasing.claim(replay, "w3", 20, 10).attempts == 1


def test_expired_worker_cannot_complete() -> None:
    """Verify expired worker cannot complete."""
    job = leasing.claim(Job("a"), "old", 0, 10)
    assert events.lookup("expired_leases")((job,), 10) == ("a",)
    queued = events.lookup("requeue_expired")(job, 10)
    reclaimed = leasing.claim(queued, "new", 10, 20)
    with pytest.raises(ValueError):
        lifecycle.complete(reclaimed, "old", 11)
    with pytest.raises(ValueError):
        leasing.heartbeat(reclaimed, "old", 11, 20)
    assert lifecycle.complete(reclaimed, "new", 11).attempts == 2


def test_tenant_isolation_across_api() -> None:
    """Verify tenant isolation across api."""
    job = submission.create_job(Submission("a", "other", "shared-key", "{}", 10))
    assert not policies.lookup("authorize")("acme", job.tenant, "admin", "cancel")
    assert routes.lookup("visible_jobs")((job,), "acme") == ()
    assert routes.lookup("page_jobs")((job,), "acme", "", 10) == ()
    assert submission.find_duplicate((job,), Submission("b", "acme", "shared-key", "{}", 10)) is None
    with pytest.raises(ValueError):
        routes.lookup("cancel")(job, "acme", 10)
    with pytest.raises(ValueError):
        lifecycle.replay(replace(job, state="dead"), "acme", 10)


def test_cancel_prevents_late_completion() -> None:
    """Verify cancel prevents late completion."""
    running = leasing.claim(Job("a"), "w", 1, 30)
    cancelled = lifecycle.cancel(running, "acme", 2)
    assert lifecycle.cancel(cancelled, "acme", 3) == cancelled
    assert leasing.expired_leases((cancelled,), 100) == ()
    assert scheduling.ready_jobs((cancelled,), 100, 10) == ()
    with pytest.raises(ValueError):
        lifecycle.complete(cancelled, "w", 3)
    assert operations.purge_candidates((cancelled,), "acme", 2) == ()
    assert operations.purge_candidates((cancelled,), "acme", 3) == ("a",)


def test_idempotency_conflict_and_canonicalization() -> None:
    """Verify idempotency conflict and canonicalization."""
    first = submission.canonical_payload('{"b":2,"a":1}')
    second = submission.canonical_payload('{"a":1, "b":2}')
    job = submission.create_job(Submission("a", "acme", "key", first, 0))
    assert submission.payload_digest(first) == submission.payload_digest(second)
    assert routes.lookup("find_duplicate")((job,), Submission("b", "acme", "key", second, 1)) == job
    with pytest.raises(ValueError):
        submission.find_duplicate((job,), Submission("b", "acme", "key", "{}", 1))
    assert job.payload == '{"a":1,"b":2}'


def test_cursor_pages_do_not_leak_or_repeat() -> None:
    """Verify cursor pages do not leak or repeat."""
    jobs = (Job("d"), Job("a"), Job("c", tenant="other"), Job("b"), Job("e"))
    first = operations.page_jobs(jobs, "acme", "", 2)
    second = operations.page_jobs(jobs, "acme", first[-1].id, 2)
    assert tuple(j.id for j in first + second) == ("a", "b", "d", "e")
    assert operations.page_jobs(jobs, "acme", second[-1].id, 2) == ()
    assert jobs[0].id == "d"


def test_backlog_and_quota_after_transition() -> None:
    """Verify backlog and quota after transition."""
    a = Job("a", due=5)
    b = Job("b", due=10)
    assert operations.oldest_ready_age((a, b), "acme", 20) == 15
    running = leasing.claim(a, "w", 20, 10)
    finished = lifecycle.complete(running, "w", 21)
    assert operations.oldest_ready_age((finished, b), "acme", 21) == 11
    assert tenancy.quota_remaining((finished, b), "acme", 2) == 1
    assert operations.state_counts((finished, b), "acme") == (("queued", 1), ("running", 0), ("succeeded", 1), ("dead", 0), ("cancelled", 0))


def test_http_backpressure_schedules_retry() -> None:
    """Verify http backpressure schedules retry."""
    running = leasing.claim(Job("a"), "w", 10, 100)
    assert policies.lookup("retryable_status")(429)
    delay = scheduling.retry_after("Thu, 01 Jan 1970 00:01:00 GMT", 20)
    assert delay == 40
    retry = lifecycle.fail(running, "w", 20, 3, delay)
    assert scheduling.ready_jobs((retry,), 59, 10) == ()
    assert scheduling.ready_jobs((retry,), 60, 10) == (retry,)


def test_retention_preserves_recoverable_work() -> None:
    """Verify retention preserves recoverable work."""
    jobs = (Job("done", state="succeeded", updated=5), Job("cancel", state="cancelled", updated=6), Job("dead", state="dead", attempts=3), Job("queued"), Job("running", state="running", owner="w", lease_until=100))
    assert events.lookup("purge_candidates")(jobs, "acme", 7) == ("cancel", "done")
    assert lifecycle.replay(jobs[2], "acme", 7).state == "queued"
    assert operations.state_counts(jobs, "acme") == (("queued", 1), ("running", 1), ("succeeded", 1), ("dead", 1), ("cancelled", 1))
