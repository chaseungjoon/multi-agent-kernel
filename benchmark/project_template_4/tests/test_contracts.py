"""Acceptance oracle with independently specified expected outcomes."""

from dataclasses import replace

import pytest

from service import (tenancy, submission, scheduling, leasing, lifecycle, operations, routes, events, policies)
from service.models import Job, Submission


def test_authorize_0() -> None:
    """Verify authorize 0."""
    assert tenancy.authorize("acme", "acme", "reader", "read")


def test_authorize_1() -> None:
    """Verify authorize 1."""
    assert not tenancy.authorize("acme", "other", "admin", "purge")


def test_authorize_2() -> None:
    """Verify authorize 2."""
    assert not tenancy.authorize("", "", "admin", "read")


def test_authorize_3() -> None:
    """Verify authorize 3."""
    assert not tenancy.authorize("acme", "acme", "reader", "cancel")


def test_authorize_4() -> None:
    """Verify authorize 4."""
    assert tenancy.authorize("acme", "acme", "operator", "submit")


def test_authorize_5() -> None:
    """Verify authorize 5."""
    assert not tenancy.authorize("acme", "acme", "admin", "unknown")


def test_authorize_6() -> None:
    """Verify authorize 6."""
    assert not tenancy.authorize("acme", "acme", "unknown", "read")


def test_visible_jobs_0() -> None:
    """Verify visible jobs 0."""
    assert tenancy.visible_jobs((Job("a"), Job("b", tenant="other"), Job("c")), "acme") == (Job("a"), Job("c"))


def test_visible_jobs_1() -> None:
    """Verify visible jobs 1."""
    assert tenancy.visible_jobs((Job("x", tenant=""),), "") == ()


def test_visible_jobs_2() -> None:
    """Verify visible jobs 2."""
    assert tenancy.visible_jobs((), "acme") == ()


def test_visible_jobs_3() -> None:
    """Verify visible jobs 3."""
    jobs = (Job("a"),)
    assert tenancy.visible_jobs(jobs, "other") == ()
    assert jobs == (Job("a"),)


def test_quota_remaining_0() -> None:
    """Verify quota remaining 0."""
    assert tenancy.quota_remaining((Job("a"), Job("b", state="running"), Job("c", state="dead"), Job("d", tenant="other")), "acme", 5) == 3


def test_quota_remaining_1() -> None:
    """Verify quota remaining 1."""
    assert tenancy.quota_remaining((Job("a"),), "acme", 0) == 0


def test_quota_remaining_2() -> None:
    """Verify quota remaining 2."""
    assert tenancy.quota_remaining((), "acme", 3) == 3


def test_quota_remaining_3() -> None:
    """Verify quota remaining 3."""
    with pytest.raises(ValueError):
        tenancy.quota_remaining((), "acme", -1)


def test_validate_tenant_0() -> None:
    """Verify validate tenant 0."""
    assert tenancy.validate_tenant("  Acme-42 ") == "acme-42"


def test_validate_tenant_1() -> None:
    """Verify validate tenant 1."""
    assert tenancy.validate_tenant("a" * 32) == "a" * 32


def test_validate_tenant_2() -> None:
    """Verify validate tenant 2."""
    with pytest.raises(ValueError):
        tenancy.validate_tenant("a" * 33)


def test_validate_tenant_3() -> None:
    """Verify validate tenant 3."""
    with pytest.raises(ValueError):
        tenancy.validate_tenant("équipe")


def test_validate_tenant_4() -> None:
    """Verify validate tenant 4."""
    with pytest.raises(ValueError):
        tenancy.validate_tenant("1ab")


def test_validate_tenant_5() -> None:
    """Verify validate tenant 5."""
    with pytest.raises(ValueError):
        tenancy.validate_tenant("ab")


def test_canonical_payload_0() -> None:
    """Verify canonical payload 0."""
    assert submission.canonical_payload('{"z": 1, "a": {"y": 2, "b": "한"}}') == '{"a":{"b":"한","y":2},"z":1}'


def test_canonical_payload_1() -> None:
    """Verify canonical payload 1."""
    assert submission.canonical_payload("{}") == "{}"


def test_canonical_payload_2() -> None:
    """Verify canonical payload 2."""
    with pytest.raises(ValueError):
        submission.canonical_payload("[]")


def test_canonical_payload_3() -> None:
    """Verify canonical payload 3."""
    with pytest.raises(ValueError):
        submission.canonical_payload("broken")


def test_canonical_payload_4() -> None:
    """Verify canonical payload 4."""
    with pytest.raises(ValueError):
        submission.canonical_payload('{"x": NaN}')


def test_canonical_payload_5() -> None:
    """Verify canonical payload 5."""
    with pytest.raises(ValueError):
        submission.canonical_payload('{"x": [1e999]}')


def test_find_duplicate_0() -> None:
    """Verify find duplicate 0."""
    assert submission.find_duplicate((Job("a", key="k", state="succeeded"),), Submission("b", "acme", "k", "{}", 10)) == Job("a", key="k", state="succeeded")


def test_find_duplicate_1() -> None:
    """Verify find duplicate 1."""
    assert submission.find_duplicate((Job("a", tenant="other", key="k"),), Submission("b", "acme", "k", "{}", 10)) is None


def test_find_duplicate_2() -> None:
    """Verify find duplicate 2."""
    assert submission.find_duplicate((Job("a"),), Submission("b", "acme", "", "{}", 10)) is None


def test_find_duplicate_3() -> None:
    """Verify find duplicate 3."""
    with pytest.raises(ValueError):
        submission.find_duplicate((Job("a", key="k", payload="x"),), Submission("b", "acme", "k", "{}", 10))


def test_find_duplicate_4() -> None:
    """Verify find duplicate 4."""
    assert submission.find_duplicate((Job("z", key="k"), Job("a", key="k")), Submission("b", "acme", "k", "{}", 0)).id == "a"


def test_create_job_0() -> None:
    """Verify create job 0."""
    assert submission.create_job(Submission("a", "other", "k", "body", 12)) == Job("a", "other", "k", "body", due=12, updated=12)


def test_create_job_1() -> None:
    """Verify create job 1."""
    assert submission.create_job(Submission("a", "acme", "", "{}", 0)) == Job("a")


def test_create_job_2() -> None:
    """Verify create job 2."""
    with pytest.raises(ValueError):
        submission.create_job(Submission("", "acme", "", "{}", 0))


def test_create_job_3() -> None:
    """Verify create job 3."""
    with pytest.raises(ValueError):
        submission.create_job(Submission("a", "", "", "{}", 0))


def test_create_job_4() -> None:
    """Verify create job 4."""
    with pytest.raises(ValueError):
        submission.create_job(Submission("a", "acme", "", "{}", -1))


def test_payload_digest_0() -> None:
    """Verify payload digest 0."""
    assert submission.payload_digest("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_payload_digest_1() -> None:
    """Verify payload digest 1."""
    assert submission.payload_digest("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_payload_digest_2() -> None:
    """Verify payload digest 2."""
    assert submission.payload_digest("{}") != submission.payload_digest("{ }")


def test_retry_delay_0() -> None:
    """Verify retry delay 0."""
    assert scheduling.retry_delay(1, 5, 60) == 5


def test_retry_delay_1() -> None:
    """Verify retry delay 1."""
    assert scheduling.retry_delay(4, 5, 60) == 40


def test_retry_delay_2() -> None:
    """Verify retry delay 2."""
    assert scheduling.retry_delay(5, 5, 60) == 60


def test_retry_delay_3() -> None:
    """Verify retry delay 3."""
    assert scheduling.retry_delay(10**12, 5, 60) == 60


def test_retry_delay_4() -> None:
    """Verify retry delay 4."""
    with pytest.raises(ValueError):
        scheduling.retry_delay(0, 5, 60)


def test_retry_delay_5() -> None:
    """Verify retry delay 5."""
    with pytest.raises(ValueError):
        scheduling.retry_delay(1, 0, 60)


def test_retry_delay_6() -> None:
    """Verify retry delay 6."""
    with pytest.raises(ValueError):
        scheduling.retry_delay(1, 5, 4)


def test_ready_jobs_0() -> None:
    """Verify ready jobs 0."""
    assert scheduling.ready_jobs((Job("b", due=2), Job("a", due=2), Job("c", due=1)), 2, 2) == (Job("c", due=1), Job("a", due=2))


def test_ready_jobs_1() -> None:
    """Verify ready jobs 1."""
    assert scheduling.ready_jobs((Job("a", due=3), Job("b", state="running")), 2, 10) == ()


def test_ready_jobs_2() -> None:
    """Verify ready jobs 2."""
    assert scheduling.ready_jobs((Job("a"),), 0, 0) == ()


def test_ready_jobs_3() -> None:
    """Verify ready jobs 3."""
    with pytest.raises(ValueError):
        scheduling.ready_jobs((), 0, -1)


def test_ready_jobs_4() -> None:
    """Verify ready jobs 4."""
    with pytest.raises(ValueError):
        scheduling.ready_jobs((), -1, 1)


def test_retry_after_0() -> None:
    """Verify retry after 0."""
    assert scheduling.retry_after(" 12 ", 0) == 12


def test_retry_after_1() -> None:
    """Verify retry after 1."""
    assert scheduling.retry_after("Thu, 01 Jan 1970 00:01:00 GMT", 10) == 50


def test_retry_after_2() -> None:
    """Verify retry after 2."""
    assert scheduling.retry_after("Thu, 01 Jan 1970 00:00:00 GMT", 10) == 0


def test_retry_after_3() -> None:
    """Verify retry after 3."""
    assert scheduling.retry_after("-2", 0) is None


def test_retry_after_4() -> None:
    """Verify retry after 4."""
    assert scheduling.retry_after("", 0) is None


def test_retry_after_5() -> None:
    """Verify retry after 5."""
    assert scheduling.retry_after("Thu, 01 Jan 1970 00:01:00", 0) is None


def test_retry_after_6() -> None:
    """Verify retry after 6."""
    with pytest.raises(ValueError):
        scheduling.retry_after("1", -1)


def test_retryable_status_0() -> None:
    """Verify retryable status 0."""
    assert all(scheduling.retryable_status(s) for s in (408, 425, 429, 500, 503, 599))


def test_retryable_status_1() -> None:
    """Verify retryable status 1."""
    assert not any(scheduling.retryable_status(s) for s in (-1, 200, 400, 401, 404, 499, 600))


def test_claim_0() -> None:
    """Verify claim 0."""
    assert leasing.claim(Job("a", due=10, attempts=2), "w", 10, 5) == Job("a", state="running", due=10, attempts=3, owner="w", lease_until=15, updated=10)


def test_claim_1() -> None:
    """Verify claim 1."""
    with pytest.raises(ValueError):
        leasing.claim(Job("a", due=11), "w", 10, 5)


def test_claim_2() -> None:
    """Verify claim 2."""
    with pytest.raises(ValueError):
        leasing.claim(Job("a", state="running"), "w", 0, 5)


def test_claim_3() -> None:
    """Verify claim 3."""
    with pytest.raises(ValueError):
        leasing.claim(Job("a"), "", 0, 5)


def test_claim_4() -> None:
    """Verify claim 4."""
    with pytest.raises(ValueError):
        leasing.claim(Job("a"), "w", 0, 0)


def test_claim_5() -> None:
    """Verify claim 5."""
    job = Job("a")
    leasing.claim(job, "w", 0, 5)
    assert job == Job("a")


def test_heartbeat_0() -> None:
    """Verify heartbeat 0."""
    assert leasing.heartbeat(Job("a", state="running", owner="w", lease_until=10), "w", 5, 20).lease_until == 25


def test_heartbeat_1() -> None:
    """Verify heartbeat 1."""
    assert leasing.heartbeat(Job("a", state="running", owner="w", lease_until=100), "w", 5, 1).lease_until == 100


def test_heartbeat_2() -> None:
    """Verify heartbeat 2."""
    with pytest.raises(ValueError):
        leasing.heartbeat(Job("a", state="running", owner="w", lease_until=10), "w", 10, 5)


def test_heartbeat_3() -> None:
    """Verify heartbeat 3."""
    with pytest.raises(ValueError):
        leasing.heartbeat(Job("a", state="running", owner="w", lease_until=10), "old", 5, 5)


def test_heartbeat_4() -> None:
    """Verify heartbeat 4."""
    with pytest.raises(ValueError):
        leasing.heartbeat(Job("a", state="running", owner="w", updated=6, lease_until=10), "w", 5, 5)


def test_expired_leases_0() -> None:
    """Verify expired leases 0."""
    assert leasing.expired_leases((Job("b", state="running", lease_until=10), Job("a", state="running", lease_until=9), Job("c", state="running", lease_until=11), Job("d")), 10) == ("a", "b")


def test_expired_leases_1() -> None:
    """Verify expired leases 1."""
    assert leasing.expired_leases((), 0) == ()


def test_expired_leases_2() -> None:
    """Verify expired leases 2."""
    with pytest.raises(ValueError):
        leasing.expired_leases((), -1)


def test_requeue_expired_0() -> None:
    """Verify requeue expired 0."""
    assert leasing.requeue_expired(Job("a", state="running", owner="w", attempts=2, lease_until=10), 10) == Job("a", attempts=2, due=10, updated=10)


def test_requeue_expired_1() -> None:
    """Verify requeue expired 1."""
    job = Job("a", state="running", owner="w", lease_until=11)
    assert leasing.requeue_expired(job, 10) == job


def test_requeue_expired_2() -> None:
    """Verify requeue expired 2."""
    job = Job("a", state="succeeded")
    assert leasing.requeue_expired(job, 10) == job


def test_requeue_expired_3() -> None:
    """Verify requeue expired 3."""
    with pytest.raises(ValueError):
        leasing.requeue_expired(Job("a"), -1)


def test_complete_0() -> None:
    """Verify complete 0."""
    assert lifecycle.complete(Job("a", state="running", owner="w", lease_until=20, attempts=1), "w", 10) == Job("a", state="succeeded", attempts=1, updated=10)


def test_complete_1() -> None:
    """Verify complete 1."""
    with pytest.raises(ValueError):
        lifecycle.complete(Job("a", state="running", owner="w", lease_until=10), "w", 10)


def test_complete_2() -> None:
    """Verify complete 2."""
    with pytest.raises(ValueError):
        lifecycle.complete(Job("a", state="running", owner="w", lease_until=20), "old", 10)


def test_complete_3() -> None:
    """Verify complete 3."""
    with pytest.raises(ValueError):
        lifecycle.complete(Job("a", state="succeeded"), "w", 0)


def test_fail_0() -> None:
    """Verify fail 0."""
    assert lifecycle.fail(Job("a", state="running", owner="w", lease_until=20, attempts=1), "w", 10, 3, 5) == Job("a", attempts=1, due=15, updated=10)


def test_fail_1() -> None:
    """Verify fail 1."""
    assert lifecycle.fail(Job("a", state="running", owner="w", lease_until=20, attempts=3, due=2), "w", 10, 3, 5) == Job("a", state="dead", attempts=3, due=2, updated=10)


def test_fail_2() -> None:
    """Verify fail 2."""
    with pytest.raises(ValueError):
        lifecycle.fail(Job("a", state="running", owner="w", lease_until=10), "w", 10, 3, 5)


def test_fail_3() -> None:
    """Verify fail 3."""
    with pytest.raises(ValueError):
        lifecycle.fail(Job("a", state="running", owner="w", lease_until=20), "w", 10, 0, 5)


def test_fail_4() -> None:
    """Verify fail 4."""
    with pytest.raises(ValueError):
        lifecycle.fail(Job("a", state="running", owner="w", lease_until=20), "w", 10, 3, -1)


def test_cancel_0() -> None:
    """Verify cancel 0."""
    assert lifecycle.cancel(Job("a", state="running", owner="w", lease_until=20), "acme", 10) == Job("a", state="cancelled", updated=10)


def test_cancel_1() -> None:
    """Verify cancel 1."""
    job = Job("a", state="cancelled", updated=1)
    assert lifecycle.cancel(job, "acme", 10) == job


def test_cancel_2() -> None:
    """Verify cancel 2."""
    with pytest.raises(ValueError):
        lifecycle.cancel(Job("a"), "other", 10)


def test_cancel_3() -> None:
    """Verify cancel 3."""
    with pytest.raises(ValueError):
        lifecycle.cancel(Job("a", state="succeeded"), "acme", 10)


def test_cancel_4() -> None:
    """Verify cancel 4."""
    with pytest.raises(ValueError):
        lifecycle.cancel(Job("a", updated=11), "acme", 10)


def test_replay_0() -> None:
    """Verify replay 0."""
    assert lifecycle.replay(Job("a", key="k", state="dead", attempts=3), "acme", 10) == Job("a", key="k", due=10, updated=10)


def test_replay_1() -> None:
    """Verify replay 1."""
    with pytest.raises(ValueError):
        lifecycle.replay(Job("a", state="dead"), "other", 10)


def test_replay_2() -> None:
    """Verify replay 2."""
    with pytest.raises(ValueError):
        lifecycle.replay(Job("a", state="succeeded"), "acme", 10)


def test_replay_3() -> None:
    """Verify replay 3."""
    with pytest.raises(ValueError):
        lifecycle.replay(Job("a", state="dead", updated=11), "acme", 10)


def test_page_jobs_0() -> None:
    """Verify page jobs 0."""
    assert operations.page_jobs((Job("c"), Job("b", tenant="other"), Job("a"), Job("d")), "acme", "a", 1) == (Job("c"),)


def test_page_jobs_1() -> None:
    """Verify page jobs 1."""
    assert operations.page_jobs((Job("a"),), "acme", "a", 100) == ()


def test_page_jobs_2() -> None:
    """Verify page jobs 2."""
    assert operations.page_jobs((Job("a", tenant=""),), "", "", 1) == ()


def test_page_jobs_3() -> None:
    """Verify page jobs 3."""
    with pytest.raises(ValueError):
        operations.page_jobs((), "acme", "", 0)


def test_page_jobs_4() -> None:
    """Verify page jobs 4."""
    with pytest.raises(ValueError):
        operations.page_jobs((), "acme", "", 101)


def test_state_counts_0() -> None:
    """Verify state counts 0."""
    assert operations.state_counts((Job("a"), Job("b"), Job("c", state="dead"), Job("d", tenant="other"), Job("e", state="unknown")), "acme") == (("queued", 2), ("running", 0), ("succeeded", 0), ("dead", 1), ("cancelled", 0))


def test_state_counts_1() -> None:
    """Verify state counts 1."""
    assert operations.state_counts((), "acme") == (("queued", 0), ("running", 0), ("succeeded", 0), ("dead", 0), ("cancelled", 0))


def test_state_counts_2() -> None:
    """Verify state counts 2."""
    assert operations.state_counts((Job("a", tenant=""),), "")[0] == ("queued", 0)


def test_purge_candidates_0() -> None:
    """Verify purge candidates 0."""
    assert operations.purge_candidates((Job("z", state="cancelled", updated=1), Job("a", state="succeeded", updated=2), Job("b", state="succeeded", updated=3), Job("c", state="dead"), Job("d"), Job("e", tenant="other", state="succeeded")), "acme", 3) == ("a", "z")


def test_purge_candidates_1() -> None:
    """Verify purge candidates 1."""
    assert operations.purge_candidates((Job("a", state="succeeded"),), "acme", 0) == ()


def test_purge_candidates_2() -> None:
    """Verify purge candidates 2."""
    with pytest.raises(ValueError):
        operations.purge_candidates((), "acme", -1)


def test_oldest_ready_age_0() -> None:
    """Verify oldest ready age 0."""
    assert operations.oldest_ready_age((Job("a", due=2), Job("b", due=5), Job("c", tenant="other", due=0), Job("d", state="running", due=0)), "acme", 10) == 8


def test_oldest_ready_age_1() -> None:
    """Verify oldest ready age 1."""
    assert operations.oldest_ready_age((Job("a", due=11),), "acme", 10) == 0


def test_oldest_ready_age_2() -> None:
    """Verify oldest ready age 2."""
    assert operations.oldest_ready_age((), "acme", 10) == 0


def test_oldest_ready_age_3() -> None:
    """Verify oldest ready age 3."""
    with pytest.raises(ValueError):
        operations.oldest_ready_age((), "acme", -1)
