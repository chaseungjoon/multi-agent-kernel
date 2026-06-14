"""Tests for mak.node_store.store."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mak.core.exceptions import NodeStoreError
from mak.core.types import NodeFragment, NodeId
from mak.node_store.store import NodeStore

SAMPLE_SOURCE = textwrap.dedent("""\
    import os

    def greet(name: str) -> str:
        return f"hello {name}"

    class Calculator:
        def add(self, a: int, b: int) -> int:
            return a + b
""")


def _frag(nid: NodeId, source: str, version: int = 1) -> NodeFragment:
    return NodeFragment(node_id=nid, kind="function", source=source, version=version)


class TestNodeStore:
    def test_parse_and_list(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        ids = store.parse_file_into_nodes("mod.py", SAMPLE_SOURCE)
        assert len(ids) >= 3
        listed = store.list_nodes("mod.py")
        assert set(ids) == set(listed)

    def test_get_node_after_parse(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        store.parse_file_into_nodes("mod.py", SAMPLE_SOURCE)
        frag = store.get_node(NodeId("mod.py::function::greet"))
        assert "greet" in frag.source
        assert frag.version == 1

    def test_get_missing_node_raises(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        with pytest.raises(NodeStoreError, match="not found"):
            store.get_node(NodeId("nonexistent::function::foo"))

    def test_put_and_commit(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("test.py::function::foo")
        store.put_node(nid, _frag(nid, "def foo(): ..."))
        store.commit_node(nid)
        assert store.get_node(nid).source == "def foo(): ..."

    def test_commit_without_put_raises(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        with pytest.raises(NodeStoreError, match="no pending"):
            store.commit_node(NodeId("fake::function::bar"))

    def test_rollback_discards_pending(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("test.py::function::foo")
        store.put_node(nid, _frag(nid, "def foo(): ..."))
        store.rollback_node(nid)
        with pytest.raises(NodeStoreError, match="not found"):
            store.get_node(nid)

    def test_put_updates_version(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("test.py::function::foo")
        store.put_node(nid, _frag(nid, "def foo(): ..."))
        store.commit_node(nid)
        store.put_node(nid, _frag(nid, "def foo(): return 1"))
        store.commit_node(nid)
        assert store.get_node(nid).version == 2

    def test_list_nodes_filters_by_file(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        store.parse_file_into_nodes("a.py", "def a(): ...\n")
        store.parse_file_into_nodes("b.py", "def b(): ...\n")
        assert all("a.py" in str(n) for n in store.list_nodes("a.py"))
        assert all("b.py" in str(n) for n in store.list_nodes("b.py"))

    def test_list_all_nodes(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        store.parse_file_into_nodes("a.py", "def a(): ...\n")
        store.parse_file_into_nodes("b.py", "def b(): ...\n")
        all_nodes = store.list_nodes()
        assert len(all_nodes) >= 2

    def test_get_committed_fragments(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        store.parse_file_into_nodes("mod.py", SAMPLE_SOURCE)
        frags = store.get_committed_fragments("mod.py")
        assert len(frags) >= 3

    def test_whole_file_node_supersedes_existing_fragments(
        self, tmp_path: Path
    ) -> None:
        # A file first ingested as fragments, then rewritten as one whole-file node,
        # must reconstruct from the whole file ALONE — not whole-file + leftover
        # fragments (which would emit every top-level symbol twice).
        store = NodeStore(tmp_path / "ns")
        store.parse_file_into_nodes("m.py", SAMPLE_SOURCE)
        assert len(store.get_committed_fragments("m.py")) >= 3  # fragments present

        whole = NodeId("m.py")
        new_src = "def only():\n    return 1\n"
        store.put_node(whole, NodeFragment(whole, "module", new_src, 1))
        store.commit_node(whole)

        frags = store.get_committed_fragments("m.py")
        assert [f.node_id for f in frags] == [whole]  # fragments gone, only the file
        assert frags[0].source == new_src
        # The old fragment nodes are no longer known to the store.
        assert all("::" not in str(n) for n in store.list_nodes("m.py"))

    def test_whole_file_node_is_listed_for_its_file(self, tmp_path: Path) -> None:
        # A bare-path node id (no ::kind::name) represents an entire new file and
        # must be returned for that file path, so reconstruction can write it.
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("editor/main.py")
        store.put_node(nid, NodeFragment(nid, "module", "def main():\n    pass\n", 1))
        store.commit_node(nid)
        assert store.list_nodes("editor/main.py") == [nid]
        frags = store.get_committed_fragments("editor/main.py")
        assert [f.node_id for f in frags] == [nid]
        assert "def main" in frags[0].source

    def test_persistence_round_trip(self, tmp_path: Path) -> None:
        store_root = tmp_path / "ns"
        store1 = NodeStore(store_root)
        store1.parse_file_into_nodes("mod.py", SAMPLE_SOURCE)

        store2 = NodeStore(store_root)
        ids = store2.list_nodes("mod.py")
        assert len(ids) >= 3
        frag = store2.get_node(NodeId("mod.py::function::greet"))
        assert "greet" in frag.source

    def test_rollback_nonexistent_is_noop(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        store.rollback_node(NodeId("fake::function::nope"))

    def test_store_owns_version_assignment(self, tmp_path: Path) -> None:
        # Risk H4: the store stamps the next version; the caller's value is ignored.
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("test.py::function::foo")
        first = store.put_node(nid, _frag(nid, "def foo(): ...", version=99))
        store.commit_node(nid)
        assert first.version == 1  # not 99
        second = store.put_node(nid, _frag(nid, "def foo(): return 1", version=0))
        store.commit_node(nid)
        assert second.version == 2

    def test_prior_versions_are_retained(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("test.py::function::foo")
        store.put_node(nid, _frag(nid, "v1 body"))
        store.commit_node(nid)
        store.put_node(nid, _frag(nid, "v2 body"))
        store.commit_node(nid)
        assert store.list_versions(nid) == [1, 2]
        assert store.get_node(nid, version=1).source == "v1 body"
        assert store.get_node(nid).source == "v2 body"

    def test_revert_to_previous_version(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("test.py::function::foo")
        store.put_node(nid, _frag(nid, "original"))
        store.commit_node(nid)
        store.put_node(nid, _frag(nid, "edited"))
        store.commit_node(nid)
        reverted = store.revert_node(nid)
        assert reverted.source == "original"
        assert store.get_node(nid).source == "original"
        assert store.get_node(nid).version == 1

    def test_get_unknown_version_raises(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("test.py::function::foo")
        store.put_node(nid, _frag(nid, "x"))
        store.commit_node(nid)
        with pytest.raises(NodeStoreError, match="version"):
            store.get_node(nid, version=5)

    def test_committed_fragments_in_source_order(self, tmp_path: Path) -> None:
        # Order must survive a reload from disk (reconstruction depends on it).
        source = "import os\n\ndef alpha(): ...\n\ndef beta(): ...\n"
        NodeStore(tmp_path / "ns").parse_file_into_nodes("m.py", source)
        reopened = NodeStore(tmp_path / "ns")
        frags = reopened.get_committed_fragments("m.py")
        assert frags[0].kind == "module_header"
        alpha_idx = next(i for i, f in enumerate(frags) if "alpha" in f.source)
        beta_idx = next(i for i, f in enumerate(frags) if "beta" in f.source)
        assert alpha_idx < beta_idx
