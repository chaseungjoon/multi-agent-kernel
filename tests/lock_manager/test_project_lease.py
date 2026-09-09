"""The project lease: single ownership, enforced by the OS (Wave 19, 19.6).

The unit-level properties live here; the cross-process ones — a second process
being refused, a killed owner releasing — are in ``test_wave19_acceptance.py``,
because they need real processes to mean anything. Threads share a file
descriptor table, so a thread-based test of ``flock`` proves nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mak.core.exceptions import ProjectBusyError
from mak.lock_manager.project_lease import (
    LEASE_FILENAME,
    LeaseOwner,
    ProjectLease,
)


class TestAcquisition:
    def test_acquiring_creates_the_lease_file_and_its_directory(
        self, tmp_path: Path
    ) -> None:
        mak_dir = tmp_path / "nested" / ".mak"
        lease = ProjectLease(mak_dir, "s1")
        lease.acquire()
        try:
            assert lease.held
            assert (mak_dir / LEASE_FILENAME).exists()
        finally:
            lease.release()

    def test_acquiring_twice_from_one_object_is_a_no_op(
        self, tmp_path: Path
    ) -> None:
        # A session initializes and may then recover; neither may deadlock the
        # other, and neither may take the lease twice.
        lease = ProjectLease(tmp_path, "s1")
        lease.acquire()
        lease.acquire()
        try:
            assert lease.held
        finally:
            lease.release()
        assert not lease.held

    def test_releasing_is_idempotent(self, tmp_path: Path) -> None:
        lease = ProjectLease(tmp_path, "s1")
        lease.acquire()
        lease.release()
        lease.release()  # safe from a finally block that already ran
        assert not lease.held

    def test_a_released_project_can_be_taken_again(self, tmp_path: Path) -> None:
        first = ProjectLease(tmp_path, "s1")
        first.acquire()
        first.release()
        second = ProjectLease(tmp_path, "s2")
        second.acquire()
        try:
            assert second.held
        finally:
            second.release()

    def test_distinct_projects_do_not_contend(self, tmp_path: Path) -> None:
        # Single ownership is per project. A global lock would make two unrelated
        # checkouts serialize for no reason.
        with ProjectLease(tmp_path / "a", "s1"), ProjectLease(tmp_path / "b", "s2"):
            pass


class TestContextManager:
    def test_it_acquires_and_releases(self, tmp_path: Path) -> None:
        lease = ProjectLease(tmp_path, "s1")
        with lease:
            assert lease.held
        assert not lease.held

    def test_it_releases_even_when_the_body_raises(self, tmp_path: Path) -> None:
        lease = ProjectLease(tmp_path, "s1")
        with pytest.raises(RuntimeError), lease:
            raise RuntimeError("boom")
        assert not lease.held
        # Which means the next owner is not locked out by a failed run.
        ProjectLease(tmp_path, "s2").acquire()


class TestOwnerRecord:
    def test_the_record_identifies_the_holder(self, tmp_path: Path) -> None:
        lease = ProjectLease(tmp_path, "session-7")
        with lease:
            record = json.loads(lease.path.read_text())
            assert record["pid"] == os.getpid()
            assert record["session_id"] == "session-7"
            assert record["hostname"]

    def test_heartbeat_advances_the_timestamp_in_place(
        self, tmp_path: Path
    ) -> None:
        lease = ProjectLease(tmp_path, "s1")
        with lease:
            inode_before = lease.path.stat().st_ino
            first = json.loads(lease.path.read_text())["heartbeat_at"]
            lease.heartbeat()
            second = json.loads(lease.path.read_text())["heartbeat_at"]
            assert second >= first
            # Written through the held descriptor, never replaced: an atomic
            # rename would swap out the inode the lock is taken on, and the next
            # process would lock a different file and find the project unguarded.
            assert lease.path.stat().st_ino == inode_before

    def test_heartbeat_without_the_lease_is_a_no_op(self, tmp_path: Path) -> None:
        ProjectLease(tmp_path, "s1").heartbeat()

    def test_a_record_with_missing_fields_still_parses(self) -> None:
        owner = LeaseOwner.from_json({"pid": 5})
        assert owner.pid == 5
        assert owner.hostname == "?"

    def test_describe_names_pid_host_and_age(self) -> None:
        owner = LeaseOwner(
            pid=42,
            hostname="box",
            session_id="s1",
            acquired_at=0.0,
            heartbeat_at=100.0,
        )
        described = owner.describe(now=130.0)
        assert "42" in described
        assert "box" in described
        assert "30s ago" in described


class TestBusyError:
    def test_the_error_names_the_project(self, tmp_path: Path) -> None:
        # The in-process case cannot contend with itself (flock is per-process),
        # so this checks the message a contended acquire builds.
        lease = ProjectLease(tmp_path, "s1")
        with lease:
            owner = json.loads(lease.path.read_text())
        error = ProjectBusyError(
            f"another MAK session already owns {tmp_path} — "
            f"{LeaseOwner.from_json(owner).describe()}."
        )
        assert str(tmp_path) in str(error)
        assert "pid" in str(error)
