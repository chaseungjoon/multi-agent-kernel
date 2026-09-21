"""Wave 19 acceptance: state preservation & truthful outcomes.

One class per numbered regression addition from the 2026-09-08 ASTRA report, in
the report's own order. Each stands for a defect that the full suite, ``ruff``,
and ``mypy --strict`` were all green *while* it held — so each test here is
written to fail against the code as it was, not merely to exercise the fix.

The invariant the first three share, and the reason they all reopen their store:
**after a failure and after a restart, disk / store / history / wave accounting
must agree on either the previous state or a fully recoverable new transaction.**
Asserting it only against the in-memory objects would pass on a store that had
advanced its memory and not its disk, which is one of the defects.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from mak.cascade import CascadeOutcome
from mak.config import MakConfig
from mak.core.exceptions import (
    NodeStoreError,
    ProjectBusyError,
    WorkTreeConflictError,
)
from mak.core.types import NodeFragment, NodeId, TaskBundle, TaskResult
from mak.execution_result import ExecutionResult
from mak.git_integration.git import GitHelper
from mak.lock_manager.project_lease import ProjectLease
from mak.node_store import journal as journal_mod
from mak.node_store import store as store_mod
from mak.node_store import transaction as transaction_mod
from mak.node_store.store import NodeStore
from mak.session import Session, SessionResult, SessionState
from mak.teardown import SuiteOutcome
from tests.test_session import (
    StagingRunner,
    _config,
    _session,
    _store,
    _task,
)

# ── shared helpers ───────────────────────────────────────────────────────────


def _reopen(store: NodeStore) -> NodeStore:
    """Open a second store over the same root — the "restart" half of the rule."""
    return NodeStore(store._root)


def _sources(store: NodeStore, file_path: str) -> str:
    """Return the source a file reconstructs to, from committed fragments."""
    return "\n".join(f.source for f in store.get_committed_fragments(file_path))


class _MultiFileRunner:
    """Stages one whole-file node per target, with per-node content."""

    def __init__(self, node_store: NodeStore, sources: dict[str, str]) -> None:
        self._store = node_store
        self._sources = sources

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        done = []
        for node_id in task.target_nodes:
            source = self._sources.get(str(node_id))
            if source is None:
                continue
            self._store.put_node(
                node_id, NodeFragment(node_id, "module", source, 1)
            )
            done.append(node_id)
        return TaskResult(
            task_id=task.task_id, success=bool(done), modified_nodes=list(done)
        )


# ── 1. a multi-file write that fails after the first file ────────────────────


class TestPartialMultiFileWrite:
    """(1) Failure on the second file, after the first was written.

    ``_reconstruct_affected`` wrote each file in turn with ``Path.write_text``.
    File one landing and file two raising left disk holding half a change whose
    store commits were then reverted — and ``write_text`` truncates before it
    writes, so the failure did not merely skip file two, it destroyed it.
    """

    def test_first_file_is_not_left_written_when_the_second_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "one.py").write_text("one = 1\n")
        (tmp_path / "two.py").write_text("two = 2\n")
        store = _store(tmp_path)
        runner = _MultiFileRunner(
            store, {"one.py": "one = 111\n", "two.py": "two = 222\n"}
        )
        session = _session(
            tmp_path, runner=runner, node_store=store, max_attempts=1
        )
        session.initialize()

        real_write = transaction_mod.write_text_atomic
        calls: list[Path] = []

        def failing_write(path: Path, text: str, **kwargs: object) -> None:
            calls.append(path)
            if len(calls) == 2:  # the second output file
                raise OSError("disk full")
            real_write(path, text)

        monkeypatch.setattr(transaction_mod, "write_text_atomic", failing_write)
        session.install_plan([_task("t", ["one.py", "two.py"])])
        result = session.run()

        assert not result.ok
        # Disk: neither file advanced. The first one is the whole point — it was
        # written before the failure and must have been rolled back.
        assert (tmp_path / "one.py").read_text() == "one = 1\n"
        assert (tmp_path / "two.py").read_text() == "two = 2\n"
        # Store, in memory and after a restart.
        for probe in (store, _reopen(store)):
            assert "one = 111" not in _sources(probe, "one.py")
            assert "two = 222" not in _sources(probe, "two.py")
        # Wave accounting agrees with both.
        assert session._wave_committed == {}

    def test_a_clean_multi_file_commit_still_writes_everything(
        self, tmp_path: Path
    ) -> None:
        # The rollback must not have been bought by never committing at all.
        (tmp_path / "one.py").write_text("one = 1\n")
        (tmp_path / "two.py").write_text("two = 2\n")
        store = _store(tmp_path)
        session = _session(
            tmp_path,
            runner=_MultiFileRunner(
                store, {"one.py": "one = 111\n", "two.py": "two = 222\n"}
            ),
            node_store=store,
            max_attempts=1,
        )
        session.initialize()
        session.install_plan([_task("t", ["one.py", "two.py"])])
        result = session.run()

        assert result.ok
        assert "one = 111" in (tmp_path / "one.py").read_text()
        assert "two = 222" in (tmp_path / "two.py").read_text()
        assert not (tmp_path / ".mak" / "journal").exists()


# ── 2. failed first-version and whole-file-replacement transactions ──────────


class TestFirstVersionAndSupersedeRollback:
    """(2) Rollback of a first-version commit, with fragment history intact.

    Two gaps in one transaction. ``revert_node`` rolls back to ``version - 1``,
    which a brand-new node has none of — so a failed first commit stayed. And
    ``commit_node`` deleted a superseded file's fragment directories *before*
    reconstruction succeeded, destroying the history the rollback needed.
    """

    def test_a_failed_first_version_commit_leaves_no_node(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        nid = NodeId("brand_new.py")
        store.put_node(nid, NodeFragment(nid, "module", "value = 1\n", 1))
        with pytest.raises(RuntimeError), store.transaction():
            store.commit_node(nid)
            raise RuntimeError("something failed after the in-memory commit")

        # There is no "version 0" to revert to; absent is the correct prior state.
        for probe in (store, _reopen(store)):
            assert nid not in probe.list_all_nodes()
            with pytest.raises(NodeStoreError):
                probe.get_node(nid)

    def test_a_failed_whole_file_commit_keeps_the_superseded_fragments(
        self, tmp_path: Path
    ) -> None:
        source = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        (tmp_path / "m.py").write_text(source)
        store = _store(tmp_path)
        store.sync_file("m.py", source)
        fragments = store.list_nodes("m.py")
        assert len(fragments) >= 2
        versions_before = {
            str(nid): store.list_versions(nid) for nid in fragments
        }

        whole = NodeId("m.py")
        store.put_node(
            whole, NodeFragment(whole, "module", "def a():\n    return 9\n", 1)
        )
        with pytest.raises(RuntimeError), store.transaction():
            store.commit_node(whole)  # supersedes every m.py:: fragment
            raise RuntimeError("reconstruction failed after commit")

        for probe in (store, _reopen(store)):
            # The fragments are back, committed, and still list for the file.
            assert set(probe.list_nodes("m.py")) == set(fragments)
            # And so is their on-disk history — the half the old code deleted
            # before it knew whether the commit would stand.
            for nid in fragments:
                assert probe.list_versions(nid) == versions_before[str(nid)]
            assert "def b" in _sources(probe, "m.py")
            assert "return 9" not in _sources(probe, "m.py")


# ── 3. a metadata save that fails after an in-memory commit ──────────────────


class TestMetadataSaveFailure:
    """(3) The store must never be left ahead of its own metadata file.

    ``commit_node`` mutated ``_nodes``/``_metadata`` and *then* called
    ``_save_metadata``, which is outside the session's try. An ``OSError`` there
    left memory holding a commit no restart would ever see.
    """

    def test_a_failed_metadata_save_rolls_the_in_memory_commit_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "m.py").write_text("value = 1\n")
        store = _store(tmp_path)
        store.sync_file("m.py", "value = 1\n")
        before = _sources(store, "m.py")

        nid = store.list_nodes("m.py")[0]
        store.put_node(nid, NodeFragment(nid, "module_body", "value = 2\n", 1))

        def boom(path: Path, text: str, **kwargs: object) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(store_mod, "write_text_atomic", boom)
        with pytest.raises(OSError, match="read-only"):
            store.commit_node(nid)
        monkeypatch.undo()

        # Memory did not advance past disk...
        assert _sources(store, "m.py") == before
        # ...and a restart agrees, which is the claim that actually matters.
        assert _sources(_reopen(store), "m.py") == before

    def test_the_same_holds_inside_a_transaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "m.py").write_text("value = 1\n")
        store = _store(tmp_path)
        store.sync_file("m.py", "value = 1\n")
        before = _sources(store, "m.py")
        nid = store.list_nodes("m.py")[0]

        def boom(path: Path, text: str, **kwargs: object) -> None:
            raise OSError("read-only filesystem")

        with pytest.raises(OSError, match="read-only"):
            with store.transaction():
                store.put_node(
                    nid, NodeFragment(nid, "module_body", "value = 3\n", 1)
                )
                store.commit_node(nid)
                monkeypatch.setattr(store_mod, "write_text_atomic", boom)
        monkeypatch.undo()

        assert _sources(store, "m.py") == before
        assert _sources(_reopen(store), "m.py") == before


# ── 4. human edits between sessions, both representations ────────────────────


class TestWorkTreeReconciliation:
    """(4) Edits, deletions, renames, and a deleted file, between two sessions.

    ``parse_file_into_nodes`` returned early when a whole-file node existed and
    ignored the source it was handed; fragment re-ingestion never removed a
    symbol that had disappeared and restamped everything at version 1. So a
    human's edit was silently discarded and a deleted function kept
    reconstructing.
    """

    def test_a_human_edit_to_a_fragment_is_adopted_and_versioned(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 1\n")
        store = _store(tmp_path)
        session = _session(
            tmp_path, runner=StagingRunner(store), node_store=store
        )
        session.initialize()
        nid = store.list_nodes("m.py")[0]
        assert store.get_node(nid).version == 1

        # A human edits the file between sessions.
        (tmp_path / "m.py").write_text("def a():\n    return 42\n")
        store2 = NodeStore(store._root)
        session2 = _session(
            tmp_path, runner=StagingRunner(store2), node_store=store2
        )
        session2.initialize()

        assert "return 42" in _sources(store2, "m.py")
        # The history continues rather than resetting to 1 — the edit is the
        # node's next version, not a brand-new node pretending to be its first.
        assert store2.get_node(nid).version == 2
        assert store2.get_node(nid, version=1).source.strip().endswith("return 1")

    def test_a_human_edit_to_a_whole_file_node_is_adopted(
        self, tmp_path: Path
    ) -> None:
        # The case that used to return early and ignore `source` entirely.
        (tmp_path / "m.py").write_text("value = 1\n")
        store = _store(tmp_path)
        whole = NodeId("m.py")
        store.put_node(whole, NodeFragment(whole, "module", "value = 1\n", 1))
        store.commit_node(whole)
        store.record_materialized("m.py", "value = 1\n")

        (tmp_path / "m.py").write_text("value = 999\n")
        session = _session(
            tmp_path, runner=StagingRunner(store), node_store=store
        )
        session.initialize()

        assert "999" in store.get_node(whole).source
        assert store.get_node(whole).version == 2

    def test_a_deleted_symbol_is_retired_and_stops_reconstructing(
        self, tmp_path: Path
    ) -> None:
        both = "def kept():\n    return 1\n\n\ndef removed():\n    return 2\n"
        (tmp_path / "m.py").write_text(both)
        store = _store(tmp_path)
        _session(
            tmp_path, runner=StagingRunner(store), node_store=store
        ).initialize()
        gone = NodeId("m.py::function::removed")
        assert gone in store.list_nodes("m.py")

        (tmp_path / "m.py").write_text("def kept():\n    return 1\n")
        store2 = NodeStore(store._root)
        _session(
            tmp_path, runner=StagingRunner(store2), node_store=store2
        ).initialize()

        assert gone not in store2.list_nodes("m.py")
        assert gone not in store2.list_all_nodes()
        assert "def removed" not in _sources(store2, "m.py")
        # Retired, not destroyed: the history is still addressable. That is the
        # difference between recording a deletion and pretending it never was.
        assert store2.is_retired(gone)
        assert "return 2" in store2.get_node(gone, version=1).source

    def test_a_renamed_symbol_retires_the_old_and_adds_the_new(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "m.py").write_text("def old_name():\n    return 1\n")
        store = _store(tmp_path)
        _session(
            tmp_path, runner=StagingRunner(store), node_store=store
        ).initialize()

        (tmp_path / "m.py").write_text("def new_name():\n    return 1\n")
        store2 = NodeStore(store._root)
        _session(
            tmp_path, runner=StagingRunner(store2), node_store=store2
        ).initialize()

        live = {str(n) for n in store2.list_nodes("m.py")}
        assert "m.py::function::new_name" in live
        assert "m.py::function::old_name" not in live
        assert "def old_name" not in _sources(store2, "m.py")

    def test_a_deleted_file_retires_every_node_it_had(self, tmp_path: Path) -> None:
        (tmp_path / "gone.py").write_text("def a():\n    return 1\n")
        (tmp_path / "stays.py").write_text("def b():\n    return 2\n")
        store = _store(tmp_path)
        _session(
            tmp_path, runner=StagingRunner(store), node_store=store
        ).initialize()
        assert store.list_nodes("gone.py")

        (tmp_path / "gone.py").unlink()
        store2 = NodeStore(store._root)
        _session(
            tmp_path, runner=StagingRunner(store2), node_store=store2
        ).initialize()

        assert store2.list_nodes("gone.py") == []
        assert store2.list_nodes("stays.py")  # untouched
        # And it does not come back on the run after that.
        store3 = NodeStore(store._root)
        _session(
            tmp_path, runner=StagingRunner(store3), node_store=store3
        ).initialize()
        assert store3.list_nodes("gone.py") == []

    def test_conflict_policy_raises_before_any_agent_sees_stale_content(
        self, tmp_path: Path
    ) -> None:
        from dataclasses import replace

        (tmp_path / "m.py").write_text("value = 1\n")
        store = _store(tmp_path)
        whole = NodeId("m.py")
        store.put_node(whole, NodeFragment(whole, "module", "value = 1\n", 1))
        store.commit_node(whole)
        store.record_materialized("m.py", "value = 1\n")
        (tmp_path / "m.py").write_text("value = 999\n")

        base = _config(tmp_path)
        config = replace(
            base, session=replace(base.session, on_external_edit="conflict")
        )
        session = _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            config=config,
        )
        with pytest.raises(WorkTreeConflictError, match="m.py"):
            session.initialize()
        # It raised during reconciliation, so the store never advanced.
        assert store.get_node(whole).source.strip() == "value = 1"


# ── 5. the user's Git index is not consumed ──────────────────────────────────


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


class TestGitIndexIsPreserved:
    """(5) Unrelated staged work is neither committed nor lost.

    ``commit_task`` ran ``git add`` on the real index, checked the *whole* index
    for staged content, then ran an unrestricted ``git commit``. A user's staged
    ``unrelated.txt`` joined MAK's audit commit, and a task with no changes of
    its own committed whatever else happened to be staged.
    """

    def test_unrelated_staged_work_is_not_committed_and_stays_staged(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        (repo / "owned.py").write_text("x = 1\n")
        (repo / "unrelated.txt").write_text("base\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

        (repo / "owned.py").write_text("x = 2\n")
        (repo / "unrelated.txt").write_text("STAGED EDIT\n")
        _git(repo, "add", "unrelated.txt")
        index_before = _git(repo, "ls-files", "--stage")

        commit = GitHelper(repo).commit_task(
            "t1", ["owned.py"], "edit owned", "fake", "s1"
        )

        assert commit is not None
        touched = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
        assert touched == ["owned.py"]
        # The user's staged edit survived, unstaged by nothing and uncommitted.
        assert "unrelated.txt" in _git(repo, "diff", "--cached", "--name-only")
        assert (repo / "unrelated.txt").read_text() == "STAGED EDIT\n"
        # owned.py's entry moved (it was committed); nothing else did.
        assert "unrelated.txt" in index_before

    def test_a_task_with_no_changes_commits_nothing_at_all(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        (repo / "owned.py").write_text("x = 1\n")
        (repo / "unrelated.txt").write_text("base\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        head_before = _git(repo, "rev-parse", "HEAD").strip()

        # Somebody else's work is staged; owned.py is untouched.
        (repo / "unrelated.txt").write_text("STAGED EDIT\n")
        _git(repo, "add", "unrelated.txt")

        commit = GitHelper(repo).commit_task(
            "t1", ["owned.py"], "no change", "fake", "s1"
        )

        assert commit is None
        assert _git(repo, "rev-parse", "HEAD").strip() == head_before
        assert "unrelated.txt" in _git(repo, "diff", "--cached", "--name-only")

    def test_a_partially_staged_target_commits_the_working_tree(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        (repo / "owned.py").write_text("x = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

        (repo / "owned.py").write_text("x = 2\n")
        _git(repo, "add", "owned.py")  # stage an intermediate version
        (repo / "owned.py").write_text("x = 3\n")  # then edit further

        GitHelper(repo).commit_task("t1", ["owned.py"], "edit", "fake", "s1")

        # Documented policy: MAK commits the working tree, not the index.
        committed = _git(repo, "show", "HEAD:owned.py")
        assert committed.strip() == "x = 3"

    def test_the_index_is_recoverable_after_a_git_failure(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path)
        (repo / "owned.py").write_text("x = 1\n")
        (repo / "unrelated.txt").write_text("base\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

        (repo / "owned.py").write_text("x = 2\n")
        (repo / "unrelated.txt").write_text("STAGED\n")
        _git(repo, "add", "unrelated.txt")
        index_before = (repo / ".git" / "index").read_bytes()

        # Make `git commit` fail: an identity it cannot resolve.
        _git(repo, "config", "--unset", "user.email")
        _git(repo, "config", "user.name", "")
        env_repo = repo
        from mak.core.exceptions import GitIntegrationError

        with pytest.raises(GitIntegrationError):
            GitHelper(env_repo).commit_task(
                "t1", ["owned.py"], "edit", "fake", "s1"
            )

        # Byte-identical: we never opened the user's index in the first place.
        assert (repo / ".git" / "index").read_bytes() == index_before
        # And no private index was left behind.
        assert list((repo / ".git").glob("mak-index-*")) == []

    def test_it_works_on_a_repo_with_no_commits_yet(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        (repo / "owned.py").write_text("x = 1\n")
        commit = GitHelper(repo).commit_task(
            "t1", ["owned.py"], "first", "fake", "s1"
        )
        assert commit is not None
        assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == [
            "owned.py"
        ]


# ── 6. the aggregate outcome, through both front ends ────────────────────────


def _result(
    *, completed: tuple[str, ...] = (), failed: tuple[str, ...] = ()
) -> SessionResult:
    return SessionResult(
        state=SessionState.FAILED if failed else SessionState.COMPLETED,
        completed=completed,
        failed=failed,
        failure_reasons={t: f"{t} broke" for t in failed},
    )


class TestAggregateOutcome:
    """(6) An early failure survives a later successful cascade wave.

    Both front ends replaced the original result with the cascade's, and
    ``install_plan`` reset the accumulators — so a failed initial wave plus a
    successful fix-up wave presented as a clean success, exit code 0, push armed.
    """

    def test_a_failed_first_wave_is_not_erased_by_a_successful_cascade(
        self,
    ) -> None:
        execution = ExecutionResult(
            initial=_result(completed=("a",), failed=("b",)),
            cascade=CascadeOutcome(waves=(_result(completed=("fix",)),)),
        )
        assert not execution.request_satisfied
        assert execution.failed == ((0, "b"),)
        # Both waves are visible, and the successful-task count is kept apart
        # from the verdict — they are different claims.
        assert execution.wave_count == 2
        assert execution.tasks_completed == 2

    def test_a_declined_cascade_is_not_a_clean_run(self) -> None:
        execution = ExecutionResult(
            initial=_result(completed=("a",)),
            cascade=CascadeOutcome(declined=True, unresolved=("fix",)),
        )
        assert not execution.request_satisfied
        assert execution.unresolved == ("fix",)

    def test_an_exhausted_cascade_is_not_a_clean_run(self) -> None:
        execution = ExecutionResult(
            initial=_result(completed=("a",)),
            cascade=CascadeOutcome(
                waves=(_result(completed=("fix",)),),
                limit_reached=True,
                unresolved=("still_broken",),
            ),
        )
        assert not execution.request_satisfied
        assert execution.cascade.limit_reached

    def test_no_cascade_matches_the_initial_result_exactly(self) -> None:
        # The common path must not change behaviour.
        assert ExecutionResult(initial=_result(completed=("a",))).request_satisfied
        assert not ExecutionResult(initial=_result(failed=("a",))).request_satisfied

    def test_the_cli_front_end_reports_a_failed_first_wave(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mak.__main__ import _report
        from mak.teardown import TeardownResult

        code = _report(
            ExecutionResult(
                initial=_result(completed=("a",), failed=("b",)),
                cascade=CascadeOutcome(waves=(_result(completed=("fix",)),)),
            ),
            TeardownResult(outcome=SuiteOutcome.PASSED),
        )
        captured = capsys.readouterr()
        assert code == 1
        assert "b" in captured.err
        assert "b broke" in captured.err

    def test_the_tui_front_end_reports_a_failed_first_wave(self) -> None:
        from cli.ui import show_results
        from rich.console import Console

        from mak.teardown import TeardownResult

        console = Console(record=True, width=100, force_terminal=False)
        show_results(
            console,
            ExecutionResult(
                initial=_result(completed=("a",), failed=("b",)),
                cascade=CascadeOutcome(waves=(_result(completed=("fix",)),)),
            ),
            TeardownResult(outcome=SuiteOutcome.PASSED),
        )
        text = console.export_text()
        assert "✗" in text          # not the green tick a clean run gets
        assert "1 failed" in text
        assert "b broke" in text

    def test_the_tui_front_end_names_a_declined_cascade(self) -> None:
        from cli.ui import show_results
        from rich.console import Console

        from mak.teardown import TeardownResult

        console = Console(record=True, width=100, force_terminal=False)
        show_results(
            console,
            ExecutionResult(
                initial=_result(completed=("a",)),
                cascade=CascadeOutcome(declined=True, unresolved=("fix",)),
            ),
            TeardownResult(outcome=SuiteOutcome.PASSED),
        )
        assert "declined" in console.export_text()

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (
                CascadeOutcome(stalled=True, stop_reason="same state"),
                "without progress",
            ),
            (
                CascadeOutcome(oscillating=True, stop_reason="A to B to A"),
                "oscillation",
            ),
            (
                CascadeOutcome(unrepairable=True, stop_reason="target removed"),
                "repair contract",
            ),
        ],
    )
    def test_the_tui_front_end_names_kernel_stopped_cascades(
        self, outcome: CascadeOutcome, expected: str
    ) -> None:
        from cli.ui import show_results
        from rich.console import Console

        from mak.teardown import TeardownResult

        console = Console(record=True, width=100, force_terminal=False)
        show_results(
            console,
            ExecutionResult(initial=_result(completed=("a",)), cascade=outcome),
            TeardownResult(outcome=SuiteOutcome.PASSED),
        )

        assert expected in console.export_text()


# ── 7. honest test status and a real push gate ───────────────────────────────


class _RecordingGit:
    """A git helper that only records whether it was asked to push."""

    def __init__(self) -> None:
        self.pushes = 0

    def push(self, branch: str | None = None, remote: str = "origin") -> None:
        self.pushes += 1


def _push_config(tmp_path: Path, *, policy: str = "require_pass") -> MakConfig:
    from dataclasses import replace

    base = _config(tmp_path)
    return replace(
        base,
        git=replace(base.git, auto_push=True, auto_commit=False),
        session=replace(base.session, test_policy=policy),
    )


class TestPushGate:
    """(7) Push behaviour for every outcome that is not a green suite.

    ``teardown`` started from ``passed = True``, so with no runner configured it
    logged ``tests_passed=True`` and pushed. It never looked at failed, blocked,
    or skipped tasks, and in the TUI an exception left the flag ``True``.
    """

    def _session_with(self, tmp_path: Path, **kwargs: object) -> Session:
        store = _store(tmp_path)
        return _session(
            tmp_path,
            runner=StagingRunner(store),
            node_store=store,
            config=_push_config(
                tmp_path, policy=str(kwargs.pop("policy", "require_pass"))
            ),
            **kwargs,  # type: ignore[arg-type]
        )

    def test_no_test_runner_is_skipped_and_does_not_push(
        self, tmp_path: Path
    ) -> None:
        git = _RecordingGit()
        session = self._session_with(tmp_path, git_helper=git, test_runner=None)
        result = session.teardown(ExecutionResult(initial=_result(completed=("a",))))

        # "No tests ran" is not "the tests passed".
        assert result.outcome is SuiteOutcome.SKIPPED
        assert not result.pushed
        assert git.pushes == 0
        assert result.push_skipped_reason is not None

    def test_a_failing_suite_does_not_push(self, tmp_path: Path) -> None:
        git = _RecordingGit()
        session = self._session_with(
            tmp_path, git_helper=git, test_runner=lambda: (False, "boom")
        )
        result = session.teardown(ExecutionResult(initial=_result(completed=("a",))))
        assert result.outcome is SuiteOutcome.FAILED
        assert git.pushes == 0

    def test_a_raising_runner_is_an_error_not_a_pass(self, tmp_path: Path) -> None:
        git = _RecordingGit()

        def explode() -> tuple[bool, str]:
            raise RuntimeError("pytest segfaulted")

        session = self._session_with(
            tmp_path, git_helper=git, test_runner=explode
        )
        result = session.teardown(ExecutionResult(initial=_result(completed=("a",))))
        assert result.outcome is SuiteOutcome.ERROR
        assert not result.ok
        assert git.pushes == 0
        assert "pytest segfaulted" in result.output

    def test_a_green_suite_over_a_failed_run_does_not_push(
        self, tmp_path: Path
    ) -> None:
        git = _RecordingGit()
        session = self._session_with(
            tmp_path, git_helper=git, test_runner=lambda: (True, "ok")
        )
        result = session.teardown(
            ExecutionResult(initial=_result(completed=("a",), failed=("b",)))
        )
        assert result.outcome is SuiteOutcome.PASSED
        assert git.pushes == 0
        assert result.push_skipped_reason is not None
        assert "did not fully succeed" in result.push_skipped_reason

    def test_a_blocked_task_does_not_push(self, tmp_path: Path) -> None:
        git = _RecordingGit()
        session = self._session_with(
            tmp_path, git_helper=git, test_runner=lambda: (True, "ok")
        )
        blocked = SessionResult(
            state=SessionState.FAILED, completed=(), failed=(), blocked=("b",)
        )
        result = session.teardown(ExecutionResult(initial=blocked))
        assert git.pushes == 0
        assert not result.pushed

    def test_a_green_suite_over_a_clean_run_pushes_exactly_once(
        self, tmp_path: Path
    ) -> None:
        git = _RecordingGit()
        session = self._session_with(
            tmp_path, git_helper=git, test_runner=lambda: (True, "ok")
        )
        result = session.teardown(ExecutionResult(initial=_result(completed=("a",))))
        assert result.outcome is SuiteOutcome.PASSED
        assert result.pushed
        assert git.pushes == 1

    def test_allow_skip_is_the_opt_out_for_a_project_with_no_suite(
        self, tmp_path: Path
    ) -> None:
        git = _RecordingGit()
        session = self._session_with(
            tmp_path, git_helper=git, test_runner=None, policy="allow_skip"
        )
        result = session.teardown(ExecutionResult(initial=_result(completed=("a",))))
        assert result.outcome is SuiteOutcome.SKIPPED
        assert result.pushed
        assert git.pushes == 1

    def test_a_declined_cascade_blocks_the_push(self, tmp_path: Path) -> None:
        git = _RecordingGit()
        session = self._session_with(
            tmp_path, git_helper=git, test_runner=lambda: (True, "ok")
        )
        result = session.teardown(
            ExecutionResult(
                initial=_result(completed=("a",)),
                cascade=CascadeOutcome(declined=True, unresolved=("fix",)),
            )
        )
        assert git.pushes == 0
        assert not result.pushed


# ── 8. one owner per project ─────────────────────────────────────────────────


def _hold_lease(mak_dir: str, ready: Any, release: Any) -> None:
    """Child process: take the lease, signal, and hold until told to stop."""
    lease = ProjectLease(Path(mak_dir), "child")
    lease.acquire()
    ready.set()
    release.wait(30)
    lease.release()


def _try_lease(mak_dir: str, result: Any) -> None:
    """Child process: report whether the project could be acquired."""
    try:
        ProjectLease(Path(mak_dir), "probe").acquire()
        result.put("acquired")
    except ProjectBusyError as exc:
        result.put(f"busy: {exc}")


class TestSingleOwner:
    """(8) Two processes, one project.

    ``LockTable`` and ``NodeStore`` guard state with an in-process ``RLock``, so
    two instances over the same files both granted a write lock on the same node.
    ``initialize`` also cleared prior leases without establishing that their owner
    was dead. Threads would prove nothing here — they share a file-descriptor
    table — so these use real processes.
    """

    def test_a_second_process_is_refused_while_the_first_holds_it(
        self, tmp_path: Path
    ) -> None:
        ctx = mp.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        result: mp.Queue = ctx.Queue()
        holder = ctx.Process(
            target=_hold_lease, args=(str(tmp_path), ready, release)
        )
        holder.start()
        try:
            assert ready.wait(30), "the holder never acquired the lease"
            probe = ctx.Process(target=_try_lease, args=(str(tmp_path), result))
            probe.start()
            probe.join(30)
            answer = result.get(timeout=10)
        finally:
            release.set()
            holder.join(30)
        assert answer.startswith("busy:")
        # The message names a suspect, not just a condition.
        assert "pid" in answer

    def test_two_distinct_projects_stay_concurrent(self, tmp_path: Path) -> None:
        one, two = tmp_path / "one", tmp_path / "two"
        with ProjectLease(one, "a"), ProjectLease(two, "b"):
            pass  # single ownership is per project, not global

    def test_an_abruptly_killed_owner_releases_the_project(
        self, tmp_path: Path
    ) -> None:
        ctx = mp.get_context("spawn")
        ready, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(
            target=_hold_lease, args=(str(tmp_path), ready, release)
        )
        holder.start()
        assert ready.wait(30), "the holder never acquired the lease"
        os.kill(holder.pid, signal.SIGKILL)  # type: ignore[arg-type]
        holder.join(30)

        # The kernel released the flock when the process died — no timeout, no
        # heuristic, which is the whole reason flock is used here.
        lease = ProjectLease(tmp_path, "successor")
        lease.acquire()
        assert lease.held
        lease.release()

    def test_acquire_release_reacquire_is_idempotent(self, tmp_path: Path) -> None:
        lease = ProjectLease(tmp_path, "s1")
        lease.acquire()
        lease.acquire()  # a no-op, not a deadlock
        lease.release()
        lease.release()  # also a no-op
        assert not lease.held
        ProjectLease(tmp_path, "s2").acquire()

    def test_a_session_takes_the_lease_before_touching_state(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "m.py").write_text("def a():\n    return 1\n")
        store = _store(tmp_path)
        held = ProjectLease(tmp_path / ".mak", "other")
        held.acquire()
        try:
            session = _session(
                tmp_path,
                runner=StagingRunner(store),
                node_store=store,
                project_lease=ProjectLease(tmp_path / ".mak", "mine"),
            )
            with pytest.raises(ProjectBusyError):
                session.initialize()
        finally:
            held.release()

    def test_the_lease_record_names_its_holder(self, tmp_path: Path) -> None:
        lease = ProjectLease(tmp_path, "session-42")
        lease.acquire()
        try:
            record = json.loads(lease.path.read_text())
            assert record["pid"] == os.getpid()
            assert record["session_id"] == "session-42"
            assert record["heartbeat_at"] >= record["acquired_at"]
            before = record["heartbeat_at"]
            time.sleep(0.01)
            lease.heartbeat()
            assert json.loads(lease.path.read_text())["heartbeat_at"] > before
        finally:
            lease.release()


# ── the invariant, stated directly ───────────────────────────────────────────


class TestConsistencyAcrossRestart:
    """After a failure *and* after a restart, every representation agrees.

    The rule the whole wave is held to. Each of the failure modes above is
    checked here against a freshly-constructed store and session over the same
    ``.mak/``, because agreement between live in-memory objects is exactly what
    the defects already had.
    """

    def test_a_rolled_back_commit_leaves_a_consistent_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "one.py").write_text("one = 1\n")
        (tmp_path / "two.py").write_text("two = 2\n")
        store = _store(tmp_path)
        session = _session(
            tmp_path,
            runner=_MultiFileRunner(
                store, {"one.py": "one = 111\n", "two.py": "two = 222\n"}
            ),
            node_store=store,
            max_attempts=1,
        )
        session.initialize()

        real_write = transaction_mod.write_text_atomic
        seen: list[Path] = []

        def failing_write(path: Path, text: str, **kwargs: object) -> None:
            seen.append(path)
            if len(seen) == 2:
                raise OSError("disk full")
            real_write(path, text)

        monkeypatch.setattr(transaction_mod, "write_text_atomic", failing_write)
        session.install_plan([_task("t", ["one.py", "two.py"])])
        session.run()
        monkeypatch.undo()

        reopened = _reopen(store)
        for name, original in (("one.py", "one = 1\n"), ("two.py", "two = 2\n")):
            on_disk = (tmp_path / name).read_text()
            assert on_disk == original                       # disk
            assert original.strip() in _sources(reopened, name)  # store
        assert session._wave_committed == {}                 # wave accounting
        assert not (tmp_path / ".mak" / "journal").exists()  # nothing in flight

    def test_an_interrupted_commit_is_resolved_on_the_next_startup(
        self, tmp_path: Path
    ) -> None:
        # Simulate a kill mid-install: a journal whose recorded versions the
        # store does not hold, so the commit point was never reached.
        (tmp_path / "m.py").write_text("value = 1\n")
        store = _store(tmp_path)
        session = _session(
            tmp_path, runner=StagingRunner(store), node_store=store
        )
        session.initialize()

        journal_dir = tmp_path / ".mak" / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / "0.bak").write_text("value = 1\n")
        journal_mod.write(
            journal_mod.CommitJournal(
                txn_id="deadbeef",
                session_id="killed",
                task_id="t",
                phase=journal_mod.PHASE_INSTALLING,
                entries=(journal_mod.JournalEntry("m.py", "0.bak"),),
                node_versions={"m.py": 99},  # a version the store never got
            ),
            journal_dir,
        )
        # A half-written file, as a killed install would leave.
        (tmp_path / "m.py").write_text("value = TRUNCA")

        store2 = NodeStore(store._root)
        _session(
            tmp_path, runner=StagingRunner(store2), node_store=store2
        ).initialize()

        assert (tmp_path / "m.py").read_text() == "value = 1\n"
        assert not journal_dir.exists()
