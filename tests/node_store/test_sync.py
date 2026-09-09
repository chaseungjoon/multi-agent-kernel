"""``NodeStore.transaction`` and ``sync_file`` (Wave 19, 19.1 + 19.2).

Two mechanisms with one job between them: never let the store's four
representations — the in-memory index, ``metadata.json``, the on-disk version
files, and the working tree — disagree about what a file contains.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mak.core.exceptions import NodeStoreError
from mak.core.types import NodeFragment, NodeId
from mak.node_store import store as store_mod
from mak.node_store.store import NodeStore

_TWO_FUNCS = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"


def _store(tmp_path: Path) -> NodeStore:
    return NodeStore(tmp_path / "store")


def _reopen(store: NodeStore) -> NodeStore:
    return NodeStore(store._root)


def _source_of(store: NodeStore, file_path: str) -> str:
    return "\n".join(f.source for f in store.get_committed_fragments(file_path))


def _metadata(store: NodeStore) -> dict[str, object]:
    path = store._root / "metadata.json"
    return json.loads(path.read_text()) if path.exists() else {}


# ── transaction ──────────────────────────────────────────────────────────────


class TestTransaction:
    def test_a_clean_transaction_commits_everything(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with store.transaction():
            for name in ("a.py", "b.py"):
                nid = NodeId(name)
                store.put_node(nid, NodeFragment(nid, "module", "x = 1\n", 1))
                store.commit_node(nid)
        assert {str(n) for n in _reopen(store).list_nodes()} == {"a.py", "b.py"}

    def test_a_failed_transaction_leaves_nothing_behind(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        nid_a = NodeId("a.py")
        store.put_node(nid_a, NodeFragment(nid_a, "module", "x = 1\n", 1))
        store.commit_node(nid_a)

        with pytest.raises(RuntimeError), store.transaction():
            nid_b = NodeId("b.py")
            store.put_node(nid_b, NodeFragment(nid_b, "module", "y = 1\n", 1))
            store.commit_node(nid_b)
            raise RuntimeError("boom")

        for probe in (store, _reopen(store)):
            assert {str(n) for n in probe.list_nodes()} == {"a.py"}

    def test_the_metadata_is_written_once_for_the_whole_transaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        writes: list[Path] = []
        real = store_mod.write_text_atomic

        def counting(path: Path, text: str, **kwargs: object) -> None:
            if path.name == "metadata.json":
                writes.append(path)
            real(path, text)

        monkeypatch.setattr(store_mod, "write_text_atomic", counting)
        with store.transaction():
            for name in ("a.py", "b.py", "c.py"):
                nid = NodeId(name)
                store.put_node(nid, NodeFragment(nid, "module", "x = 1\n", 1))
                store.commit_node(nid)
        assert len(writes) == 1

    def test_pruning_is_deferred_until_the_commit_point(
        self, tmp_path: Path
    ) -> None:
        # Pruning deletes the very version files a rollback restores the index
        # to, so it cannot run before the transaction is known to stand.
        store = NodeStore(tmp_path / "store", version_retention=2)
        nid = NodeId("a.py")
        for value in range(1, 4):
            store.put_node(nid, NodeFragment(nid, "module", f"x = {value}\n", 1))
            store.commit_node(nid)
        assert store.get_node(nid).version == 3
        versions_before = store.list_versions(nid)

        with pytest.raises(RuntimeError), store.transaction():
            store.put_node(nid, NodeFragment(nid, "module", "x = 4\n", 1))
            store.commit_node(nid)
            raise RuntimeError("boom")

        # Nothing the rollback needed was pruned: the committed version and the
        # one below it are both still on disk, so the index landed on real files.
        assert set(versions_before) <= set(store.list_versions(nid))
        assert store.get_node(nid).version == 3
        assert "x = 3" in store.get_node(nid).source
        # The staged v4 file is still there — discarding a *pending* fragment is
        # ``rollback_node``'s job, not the transaction's, and the session's
        # failure path calls it. The transaction restored ``_pending``, so it can.
        store.rollback_node(nid)
        assert store.list_versions(nid) == versions_before

    def test_nested_transactions_commit_once_at_the_outermost(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        with store.transaction():
            with store.transaction():
                nid = NodeId("a.py")
                store.put_node(nid, NodeFragment(nid, "module", "x = 1\n", 1))
                store.commit_node(nid)
            # The inner block exiting is not a commit point.
            assert _metadata(store) == {}
        assert "a.py" in _metadata(store)

    def test_an_inner_failure_rolls_the_whole_thing_back(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        with pytest.raises(RuntimeError), store.transaction():
            nid = NodeId("a.py")
            store.put_node(nid, NodeFragment(nid, "module", "x = 1\n", 1))
            store.commit_node(nid)
            with store.transaction():
                raise RuntimeError("boom")
        assert _reopen(store).list_nodes() == []


class TestUncommitNode:
    def test_it_returns_a_first_version_node_to_absent(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        nid = NodeId("a.py")
        store.put_node(nid, NodeFragment(nid, "module", "x = 1\n", 1))
        store.commit_node(nid)
        store.uncommit_node(nid)
        for probe in (store, _reopen(store)):
            with pytest.raises(NodeStoreError):
                probe.get_node(nid)

    def test_it_leaves_the_on_disk_versions_addressable(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        nid = NodeId("a.py")
        store.put_node(nid, NodeFragment(nid, "module", "x = 1\n", 1))
        store.commit_node(nid)
        store.uncommit_node(nid)
        # A rollback that destroyed the fragment would make the failure
        # destructive; the id stays usable for the next attempt.
        assert store.list_versions(nid) == [1]

    def test_uncommitting_an_unknown_node_is_harmless(
        self, tmp_path: Path
    ) -> None:
        _store(tmp_path).uncommit_node(NodeId("never.py"))


# ── sync_file ────────────────────────────────────────────────────────────────


class TestSyncNewFile:
    def test_a_new_file_ingests_at_version_one(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        report = store.sync_file("m.py", _TWO_FUNCS)
        assert len(report.added) == 2
        assert not report.updated and not report.retired
        for nid in report.added:
            assert store.get_node(nid).version == 1

    def test_an_unchanged_file_churns_no_versions(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.sync_file("m.py", _TWO_FUNCS)
        report = store.sync_file("m.py", _TWO_FUNCS)
        assert not report.changed
        assert len(report.unchanged) == 2
        for nid in report.unchanged:
            assert store.get_node(nid).version == 1


class TestSyncFragmentEdits:
    def test_an_edited_symbol_advances_its_version(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.sync_file("m.py", _TWO_FUNCS)
        edited = _TWO_FUNCS.replace("return 1", "return 111")
        report = store.sync_file("m.py", edited)

        assert len(report.updated) == 1
        nid = report.updated[0]
        # The history continues. Re-ingestion used to stamp version 1 every
        # time, so a node's edit history reset on every run.
        assert store.get_node(nid).version == 2
        assert "return 1\n" in store.get_node(nid, version=1).source
        assert "return 111" in _source_of(store, "m.py")

    def test_a_deleted_symbol_is_retired(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.sync_file("m.py", _TWO_FUNCS)
        report = store.sync_file("m.py", "def a():\n    return 1\n")

        assert len(report.retired) == 1
        gone = report.retired[0]
        assert str(gone).endswith("::b")
        assert gone not in store.list_nodes("m.py")
        assert gone not in store.list_all_nodes()
        assert "def b" not in _source_of(store, "m.py")
        # Retired, not removed: history stays readable.
        assert store.is_retired(gone)
        assert "return 2" in store.get_node(gone, version=1).source

    def test_a_retired_symbol_stays_retired_across_a_restart(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.sync_file("m.py", _TWO_FUNCS)
        store.sync_file("m.py", "def a():\n    return 1\n")
        reopened = _reopen(store)
        assert "def b" not in _source_of(reopened, "m.py")
        assert len(reopened.list_nodes("m.py")) == 1

    def test_a_re_added_symbol_comes_back_live(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.sync_file("m.py", _TWO_FUNCS)
        store.sync_file("m.py", "def a():\n    return 1\n")
        report = store.sync_file("m.py", _TWO_FUNCS)
        back = NodeId("m.py::function::b")
        assert back in store.list_nodes("m.py")
        assert not store.is_retired(back)
        assert "def b" in _source_of(store, "m.py")
        assert report.changed

    def test_a_new_symbol_is_added_alongside_the_existing_ones(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.sync_file("m.py", _TWO_FUNCS)
        report = store.sync_file(
            "m.py", _TWO_FUNCS + "\n\ndef c():\n    return 3\n"
        )
        assert [str(n) for n in report.added] == ["m.py::function::c"]
        assert "def c" in _source_of(store, "m.py")


class TestSyncWholeFile:
    def test_a_whole_file_node_adopts_the_supplied_source(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        nid = NodeId("m.py")
        store.put_node(nid, NodeFragment(nid, "module", "value = 1\n", 1))
        store.commit_node(nid)

        # The case that used to return early and ignore `source` entirely, so a
        # human's edit to a whole-file node was silently reverted.
        report = store.sync_file("m.py", "value = 999\n")

        assert report.updated == (nid,)
        assert "999" in store.get_node(nid).source
        assert store.get_node(nid).version == 2

    def test_an_unchanged_whole_file_node_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        nid = NodeId("m.py")
        store.put_node(nid, NodeFragment(nid, "module", "value = 1\n", 1))
        store.commit_node(nid)
        report = store.sync_file("m.py", "value = 1\n")
        assert report.unchanged == (nid,)
        assert store.get_node(nid).version == 1

    def test_it_stays_whole_file_rather_than_re_fragmenting(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        nid = NodeId("m.py")
        store.put_node(nid, NodeFragment(nid, "module", "def a():\n    return 1\n", 1))
        store.commit_node(nid)
        store.sync_file("m.py", _TWO_FUNCS)
        # Re-fragmenting would create the stale siblings the old early return
        # was (badly) avoiding: the whole-file node remains authoritative.
        assert store.list_nodes("m.py") == [nid]


class TestSyncDeletedFile:
    def test_a_missing_file_retires_all_of_its_nodes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.sync_file("m.py", _TWO_FUNCS)
        report = store.sync_file("m.py", None)
        assert len(report.retired) == 2
        assert store.list_nodes("m.py") == []
        assert store.materialized_digest("m.py") is None

    def test_syncing_an_unknown_missing_file_does_nothing(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        assert not store.sync_file("never.py", None).changed


class TestMaterializationRecord:
    def test_recording_and_reading_a_digest(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.materialized_digest("m.py") is None
        store.record_materialized("m.py", "value = 1\n")
        assert store.materialized_digest("m.py") == NodeStore.content_digest(
            "value = 1\n"
        )

    def test_it_survives_a_restart(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.record_materialized("m.py", "value = 1\n")
        assert _reopen(store).materialized_digest("m.py") is not None

    def test_an_unreadable_sidecar_reads_as_never_seen(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.record_materialized("m.py", "value = 1\n")
        (store._root / "file_state.json").write_text("{ truncated")
        # Every file reads as unseen, which makes reconciliation treat the tree
        # as authoritative — the safe direction to be wrong in.
        assert _reopen(store).materialized_digest("m.py") is None


class TestGcKeepsRetiredHistory:
    def test_a_retired_node_is_not_swept_as_an_orphan(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.sync_file("m.py", _TWO_FUNCS)
        store.sync_file("m.py", "def a():\n    return 1\n")
        gone = NodeId("m.py::function::b")
        store.gc()
        # The id stays in metadata precisely so the forward-mapping orphan sweep
        # still protects its directory.
        assert "return 2" in store.get_node(gone, version=1).source
