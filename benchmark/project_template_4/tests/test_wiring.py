"""Acceptance oracle with independently specified expected outcomes."""

from dataclasses import replace

import pytest

from service import (tenancy, submission, scheduling, leasing, lifecycle, operations, routes, events, policies)
from service.models import Job, Submission


def test_routes_authorize() -> None:
    """Verify routes authorize."""
    assert routes.lookup("authorize") is tenancy.authorize


def test_policies_authorize() -> None:
    """Verify policies authorize."""
    assert policies.lookup("authorize") is tenancy.authorize


def test_routes_visible_jobs() -> None:
    """Verify routes visible jobs."""
    assert routes.lookup("visible_jobs") is tenancy.visible_jobs


def test_policies_quota_remaining() -> None:
    """Verify policies quota remaining."""
    assert policies.lookup("quota_remaining") is tenancy.quota_remaining


def test_routes_find_duplicate() -> None:
    """Verify routes find duplicate."""
    assert routes.lookup("find_duplicate") is submission.find_duplicate


def test_policies_find_duplicate() -> None:
    """Verify policies find duplicate."""
    assert policies.lookup("find_duplicate") is submission.find_duplicate


def test_routes_create_job() -> None:
    """Verify routes create job."""
    assert routes.lookup("create_job") is submission.create_job


def test_events_create_job() -> None:
    """Verify events create job."""
    assert events.lookup("create_job") is submission.create_job


def test_policies_retry_delay() -> None:
    """Verify policies retry delay."""
    assert policies.lookup("retry_delay") is scheduling.retry_delay


def test_routes_ready_jobs() -> None:
    """Verify routes ready jobs."""
    assert routes.lookup("ready_jobs") is scheduling.ready_jobs


def test_events_ready_jobs() -> None:
    """Verify events ready jobs."""
    assert events.lookup("ready_jobs") is scheduling.ready_jobs


def test_policies_retryable_status() -> None:
    """Verify policies retryable status."""
    assert policies.lookup("retryable_status") is scheduling.retryable_status


def test_routes_claim() -> None:
    """Verify routes claim."""
    assert routes.lookup("claim") is leasing.claim


def test_events_claim() -> None:
    """Verify events claim."""
    assert events.lookup("claim") is leasing.claim


def test_routes_heartbeat() -> None:
    """Verify routes heartbeat."""
    assert routes.lookup("heartbeat") is leasing.heartbeat


def test_events_expired_leases() -> None:
    """Verify events expired leases."""
    assert events.lookup("expired_leases") is leasing.expired_leases


def test_policies_expired_leases() -> None:
    """Verify policies expired leases."""
    assert policies.lookup("expired_leases") is leasing.expired_leases


def test_events_requeue_expired() -> None:
    """Verify events requeue expired."""
    assert events.lookup("requeue_expired") is leasing.requeue_expired


def test_routes_complete() -> None:
    """Verify routes complete."""
    assert routes.lookup("complete") is lifecycle.complete


def test_events_complete() -> None:
    """Verify events complete."""
    assert events.lookup("complete") is lifecycle.complete


def test_events_fail() -> None:
    """Verify events fail."""
    assert events.lookup("fail") is lifecycle.fail


def test_policies_fail() -> None:
    """Verify policies fail."""
    assert policies.lookup("fail") is lifecycle.fail


def test_routes_cancel() -> None:
    """Verify routes cancel."""
    assert routes.lookup("cancel") is lifecycle.cancel


def test_routes_replay() -> None:
    """Verify routes replay."""
    assert routes.lookup("replay") is lifecycle.replay


def test_events_replay() -> None:
    """Verify events replay."""
    assert events.lookup("replay") is lifecycle.replay


def test_routes_page_jobs() -> None:
    """Verify routes page jobs."""
    assert routes.lookup("page_jobs") is operations.page_jobs


def test_routes_state_counts() -> None:
    """Verify routes state counts."""
    assert routes.lookup("state_counts") is operations.state_counts


def test_events_state_counts() -> None:
    """Verify events state counts."""
    assert events.lookup("state_counts") is operations.state_counts


def test_events_purge_candidates() -> None:
    """Verify events purge candidates."""
    assert events.lookup("purge_candidates") is operations.purge_candidates


def test_policies_purge_candidates() -> None:
    """Verify policies purge candidates."""
    assert policies.lookup("purge_candidates") is operations.purge_candidates
