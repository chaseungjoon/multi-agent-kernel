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
        frag = NodeFragment(node_id=nid, kind="function", source="def foo(): ...", version=1)
        store.put_node(nid, frag)
        store.commit_node(nid)
        assert store.get_node(nid).source == "def foo(): ..."

    def test_commit_without_put_raises(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        with pytest.raises(NodeStoreError, match="no pending"):
            store.commit_node(NodeId("fake::function::bar"))

    def test_rollback_discards_pending(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("test.py::function::foo")
        frag = NodeFragment(node_id=nid, kind="function", source="def foo(): ...", version=1)
        store.put_node(nid, frag)
        store.rollback_node(nid)
        with pytest.raises(NodeStoreError, match="not found"):
            store.get_node(nid)

    def test_put_updates_version(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ns")
        nid = NodeId("test.py::function::foo")
        v1 = NodeFragment(node_id=nid, kind="function", source="def foo(): ...", version=1)
        store.put_node(nid, v1)
        store.commit_node(nid)
        v2 = NodeFragment(node_id=nid, kind="function", source="def foo(): return 1", version=2)
        store.put_node(nid, v2)
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
