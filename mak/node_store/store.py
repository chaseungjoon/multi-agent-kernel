"""NodeStore: versioned AST fragment storage with commit/rollback semantics."""

from __future__ import annotations

import json
from pathlib import Path

from mak.core.exceptions import NodeStoreError
from mak.core.types import NodeFragment, NodeId
from mak.node_store.ingestion import parse_file_into_fragments


class NodeStore:
    """Versioned fragment store backed by .mak/node_store/ on disk."""

    def __init__(self, store_root: Path) -> None:
        self._root = store_root
        self._root.mkdir(parents=True, exist_ok=True)

        self._nodes: dict[NodeId, NodeFragment] = {}
        self._pending: dict[NodeId, NodeFragment] = {}
        self._metadata: dict[NodeId, dict[str, object]] = {}

        self._load_from_disk()

    def _fragment_dir(self, node_id: NodeId) -> Path:
        parts = str(node_id).replace("::", "/")
        return self._root / parts

    def _load_from_disk(self) -> None:
        if not self._root.exists():
            return
        meta_path = self._root / "metadata.json"
        if meta_path.exists():
            data = json.loads(meta_path.read_text("utf-8"))
            for nid_str, meta in data.items():
                nid = NodeId(nid_str)
                self._metadata[nid] = meta
                frag_dir = self._fragment_dir(nid)
                version = int(meta.get("version", 1))
                frag_file = frag_dir / f"v{version}.py"
                if frag_file.exists():
                    source = frag_file.read_text("utf-8")
                    self._nodes[nid] = NodeFragment(
                        node_id=nid,
                        kind=str(meta.get("kind", "unknown")),
                        source=source,
                        version=version,
                    )

    def _save_metadata(self) -> None:
        meta_path = self._root / "metadata.json"
        data = {str(k): v for k, v in self._metadata.items()}
        meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _write_fragment_to_disk(self, fragment: NodeFragment) -> None:
        frag_dir = self._fragment_dir(fragment.node_id)
        frag_dir.mkdir(parents=True, exist_ok=True)
        frag_file = frag_dir / f"v{fragment.version}.py"
        frag_file.write_text(fragment.source, encoding="utf-8")

    def get_node(self, node_id: NodeId) -> NodeFragment:
        """Return the latest committed fragment for a node."""
        if node_id in self._nodes:
            return self._nodes[node_id]
        raise NodeStoreError(f"node not found: {node_id}")

    def put_node(self, node_id: NodeId, fragment: NodeFragment) -> None:
        """Stage a new version of a node (uncommitted)."""
        self._pending[node_id] = fragment
        self._write_fragment_to_disk(fragment)

    def commit_node(self, node_id: NodeId) -> None:
        """Promote a pending fragment to committed."""
        if node_id not in self._pending:
            raise NodeStoreError(f"no pending version for node: {node_id}")
        fragment = self._pending.pop(node_id)
        self._nodes[node_id] = fragment
        self._metadata[node_id] = {
            "kind": fragment.kind,
            "version": fragment.version,
        }
        self._save_metadata()

    def rollback_node(self, node_id: NodeId) -> None:
        """Discard a pending fragment."""
        if node_id in self._pending:
            frag = self._pending.pop(node_id)
            frag_dir = self._fragment_dir(node_id)
            frag_file = frag_dir / f"v{frag.version}.py"
            if frag_file.exists():
                frag_file.unlink()

    def list_nodes(self, file_path: str | None = None) -> list[NodeId]:
        """List all committed node IDs, optionally filtered by file path."""
        if file_path is None:
            return list(self._nodes.keys())
        prefix = f"{file_path}::"
        return [nid for nid in self._nodes if str(nid).startswith(prefix)]

    def parse_file_into_nodes(self, file_path: str, source: str | None = None) -> list[NodeId]:
        """Parse a file, store all fragments, and return node IDs."""
        fragments = parse_file_into_fragments(file_path, source)
        node_ids: list[NodeId] = []
        for frag in fragments:
            self._pending[frag.node_id] = frag
            self._write_fragment_to_disk(frag)
            self.commit_node(frag.node_id)
            node_ids.append(frag.node_id)
        return node_ids

    def get_committed_fragments(self, file_path: str) -> list[NodeFragment]:
        """Return all committed fragments for a file in stored order."""
        node_ids = self.list_nodes(file_path)
        fragments: list[NodeFragment] = []
        for nid in node_ids:
            fragments.append(self._nodes[nid])
        return fragments
