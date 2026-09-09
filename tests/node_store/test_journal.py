"""The commit journal and its three recovery branches (Wave 19, 19.1).

A journal exists for the failure nobody is present for: the process is gone, and
a *later* one has to work out what it was doing. So these tests never exercise
recovery through the code that wrote the journal — they write one by hand, in
each of the states a kill can leave, and hand it to a fresh store.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from mak.core.types import NodeFragment, NodeId
from mak.node_store import journal as journal_mod
from mak.node_store import transaction as transaction_mod
from mak.node_store.store import NodeStore


def _store(tmp_path: Path) -> NodeStore:
    return NodeStore(tmp_path / "store")


def _resolver(tmp_path: Path) -> Callable[[str], Path]:
    def resolve(file_path: str) -> Path:
        return tmp_path / file_path

    return resolve


def _committed_store(tmp_path: Path, source: str) -> tuple[NodeStore, NodeId]:
    """Build a store holding ``m.py`` as one whole-file node at version 1."""
    store = _store(tmp_path)
    nid = NodeId("m.py")
    store.put_node(nid, NodeFragment(nid, "module", source, 1))
    store.commit_node(nid)
    return store, nid


def _write_journal(
    journal_dir: Path,
    *,
    phase: str,
    node_versions: dict[str, int],
    backup: str | None = "0.bak",
    backup_content: str | None = "original\n",
) -> None:
    journal_dir.mkdir(parents=True, exist_ok=True)
    if backup is not None and backup_content is not None:
        (journal_dir / backup).write_text(backup_content)
    journal_mod.write(
        journal_mod.CommitJournal(
            txn_id="txn1",
            session_id="killed",
            task_id="t",
            phase=phase,
            entries=(journal_mod.JournalEntry("m.py", backup),),
            node_versions=node_versions,
        ),
        journal_dir,
    )


class TestRoundTrip:
    def test_a_journal_survives_write_and_read(self, tmp_path: Path) -> None:
        record = journal_mod.CommitJournal(
            txn_id="abc",
            session_id="s1",
            task_id="t1",
            phase=journal_mod.PHASE_INSTALLING,
            entries=(
                journal_mod.JournalEntry("a.py", "0.bak"),
                journal_mod.JournalEntry("b.py", None),
            ),
            node_versions={"a.py": 3},
        )
        journal_mod.write(record, tmp_path)
        assert journal_mod.read(tmp_path) == record

    def test_no_journal_reads_as_nothing_in_flight(self, tmp_path: Path) -> None:
        assert journal_mod.read(tmp_path) is None

    def test_an_unreadable_journal_is_quarantined_not_raised(
        self, tmp_path: Path
    ) -> None:
        # A truncated journal is exactly what a kill mid-write produces, and it
        # must not be able to stop the session that exists to recover from it.
        journal_mod.journal_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        journal_mod.journal_path(tmp_path).write_text('{"txn_id": "a"')
        assert journal_mod.read(tmp_path) is None
        assert (tmp_path / "journal.json.corrupt").exists()

    def test_discard_removes_the_backups_too(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, phase=journal_mod.PHASE_INSTALLING, node_versions={})
        assert (tmp_path / "0.bak").exists()
        journal_mod.discard(tmp_path)
        assert not tmp_path.exists()

    def test_discarding_nothing_is_not_an_error(self, tmp_path: Path) -> None:
        journal_mod.discard(tmp_path / "never-existed")


class TestBackup:
    def test_an_existing_file_is_backed_up(self, tmp_path: Path) -> None:
        target = tmp_path / "m.py"
        target.write_text("before\n")
        name = journal_mod.back_up(tmp_path / "j", 0, target)
        assert name == "0.bak"
        assert (tmp_path / "j" / "0.bak").read_text() == "before\n"

    def test_a_missing_file_records_none(self, tmp_path: Path) -> None:
        # None means "delete on rollback", which is the right answer for a task
        # that creates a new module — a backup-only scheme cannot say it.
        assert journal_mod.back_up(tmp_path / "j", 0, tmp_path / "absent.py") is None


class TestCommitPointDetection:
    def test_matching_versions_mean_the_commit_landed(self, tmp_path: Path) -> None:
        store, nid = _committed_store(tmp_path, "value = 1\n")
        _write_journal(
            tmp_path / "j",
            phase=journal_mod.PHASE_INSTALLING,
            node_versions={"m.py": 1},
        )
        record = journal_mod.read(tmp_path / "j")
        assert record is not None
        assert journal_mod.commit_point_passed(
            record, lambda n: store.get_node(n).version
        )

    def test_a_differing_version_means_it_did_not(self, tmp_path: Path) -> None:
        store, _ = _committed_store(tmp_path, "value = 1\n")
        _write_journal(
            tmp_path / "j",
            phase=journal_mod.PHASE_INSTALLING,
            node_versions={"m.py": 7},
        )
        record = journal_mod.read(tmp_path / "j")
        assert record is not None
        assert not journal_mod.commit_point_passed(
            record, lambda n: store.get_node(n).version
        )


class TestRecoveryBranches:
    """The three states a kill can leave, and what each must resolve to."""

    def test_nothing_in_flight_recovers_nothing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert (
            transaction_mod.recover(
                tmp_path / "j", store, resolve=_resolver(tmp_path)
            )
            is None
        )

    def test_an_uncommitted_transaction_rolls_back(self, tmp_path: Path) -> None:
        store, _ = _committed_store(tmp_path, "value = 1\n")
        # A half-written destination, as a killed install leaves.
        (tmp_path / "m.py").write_text("value = TRUNCA")
        _write_journal(
            tmp_path / "j",
            phase=journal_mod.PHASE_INSTALLING,
            node_versions={"m.py": 99},  # a version the store never reached
            backup_content="value = 1\n",
        )

        branch = transaction_mod.recover(
            tmp_path / "j", store, resolve=_resolver(tmp_path)
        )

        assert branch == "rolled-back"
        assert (tmp_path / "m.py").read_text() == "value = 1\n"
        assert not (tmp_path / "j").exists()

    def test_a_committed_transaction_rolls_forward(self, tmp_path: Path) -> None:
        store, _ = _committed_store(tmp_path, "value = 2\n")
        # Killed before the file was written, but *after* the metadata save —
        # the store is the authority and the file must catch up to it.
        (tmp_path / "m.py").write_text("value = 1\n")
        _write_journal(
            tmp_path / "j",
            phase=journal_mod.PHASE_INSTALLING,
            node_versions={"m.py": 1},  # matches what the store holds
            backup_content="value = 1\n",
        )

        branch = transaction_mod.recover(
            tmp_path / "j", store, resolve=_resolver(tmp_path)
        )

        assert branch == "rolled-forward"
        assert "value = 2" in (tmp_path / "m.py").read_text()
        assert not (tmp_path / "j").exists()
        # Rolling forward also records what it materialized, so the next
        # session does not mistake its own write for a human's edit.
        assert store.materialized_digest("m.py") is not None

    def test_a_rollback_deletes_a_file_that_did_not_exist_before(
        self, tmp_path: Path
    ) -> None:
        store, _ = _committed_store(tmp_path, "value = 1\n")
        (tmp_path / "m.py").write_text("half written")
        _write_journal(
            tmp_path / "j",
            phase=journal_mod.PHASE_INSTALLING,
            node_versions={"m.py": 99},
            backup=None,
            backup_content=None,
        )

        transaction_mod.recover(
            tmp_path / "j", store, resolve=_resolver(tmp_path)
        )

        assert not (tmp_path / "m.py").exists()

    def test_an_installed_phase_reruns_only_the_audit(
        self, tmp_path: Path
    ) -> None:
        store, _ = _committed_store(tmp_path, "value = 2\n")
        (tmp_path / "m.py").write_text("value = 2\n")
        _write_journal(
            tmp_path / "j",
            phase=journal_mod.PHASE_INSTALLED,
            node_versions={"m.py": 1},
        )
        seen: list[tuple[str, list[str]]] = []

        branch = transaction_mod.recover(
            tmp_path / "j",
            store,
            resolve=_resolver(tmp_path),
            reaudit=lambda task_id, files: seen.append((task_id, files)),
        )

        assert branch == "reaudited"
        assert seen == [("t", ["m.py"])]
        assert not (tmp_path / "j").exists()

    def test_a_failing_reaudit_does_not_block_startup(self, tmp_path: Path) -> None:
        store, _ = _committed_store(tmp_path, "value = 2\n")
        (tmp_path / "m.py").write_text("value = 2\n")
        _write_journal(
            tmp_path / "j",
            phase=journal_mod.PHASE_INSTALLED,
            node_versions={"m.py": 1},
        )

        def explode(task_id: str, files: list[str]) -> None:
            raise RuntimeError("git is broken")

        # The change itself is durable; only its audit-log entry is in doubt, so
        # a git failure here is a warning, never a reason to refuse to start.
        assert (
            transaction_mod.recover(
                tmp_path / "j", store, resolve=_resolver(tmp_path), reaudit=explode
            )
            == "reaudited"
        )
        assert not (tmp_path / "j").exists()


class TestInstallFiles:
    def test_a_clean_install_writes_and_journals(self, tmp_path: Path) -> None:
        store, nid = _committed_store(tmp_path, "value = 1\n")
        installed = transaction_mod.install_files(
            store,
            [nid],
            resolve=_resolver(tmp_path),
            journal_dir=tmp_path / "j",
            session_id="s1",
            task_id="t1",
        )
        assert (tmp_path / "m.py").read_text().strip() == "value = 1"
        assert installed.journal.node_versions == {"m.py": 1}
        assert json.loads(
            journal_mod.journal_path(tmp_path / "j").read_text()
        )["phase"] == journal_mod.PHASE_INSTALLING

        transaction_mod.mark_installed(installed)
        assert json.loads(
            journal_mod.journal_path(tmp_path / "j").read_text()
        )["phase"] == journal_mod.PHASE_INSTALLED

        transaction_mod.finish(installed)
        assert not (tmp_path / "j").exists()

    def test_a_file_with_no_fragments_fails_before_writing_anything(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        with pytest.raises(OSError, match="no committed fragments"):
            transaction_mod.install_files(
                store,
                [NodeId("absent.py")],
                resolve=_resolver(tmp_path),
                journal_dir=tmp_path / "j",
                session_id="s1",
                task_id="t1",
            )
        assert not (tmp_path / "j").exists()
        assert not (tmp_path / "absent.py").exists()
