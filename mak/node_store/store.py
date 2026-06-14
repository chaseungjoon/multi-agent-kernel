"""NodeStore: versioned AST fragment storage with commit/rollback semantics.

The store owns version assignment: ``put_node`` ignores any version on
the incoming fragment and stamps it ``current_committed + 1``, so callers never
have to guess the next version. Prior versions are retained on disk, enabling
``revert_node`` (roll a committed node back to its previous version). Fragment
order is preserved as ``order`` metadata so reconstruction emits source in its
original order. All mutations are guarded by a re-entrant lock.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path

from mak.core.exceptions import NodeStoreError
from mak.core.types import NodeFragment, NodeId
from mak.node_store.ingestion import parse_file_into_fragments


class NodeStore:
    """Versioned fragment store backed by ``.mak/node_store/`` on disk."""

    def __init__(self, store_root: Path) -> None:
        self._root = store_root
        self._root.mkdir(parents=True, exist_ok=True)

        self._nodes: dict[NodeId, NodeFragment] = {}
        self._pending: dict[NodeId, NodeFragment] = {}
        self._metadata: dict[NodeId, dict[str, object]] = {}
        self._lock = threading.RLock()

        self._load_from_disk()

    def _fragment_dir(self, node_id: NodeId) -> Path:
        return self._root / str(node_id).replace("::", "/")

    def _load_from_disk(self) -> None:
        meta_path = self._root / "metadata.json"
        if not meta_path.exists():
            return
        data = json.loads(meta_path.read_text("utf-8"))
        for nid_str, meta in data.items():
            nid = NodeId(nid_str)
            self._metadata[nid] = meta
            version = int(meta.get("version", 1))
            frag_file = self._fragment_dir(nid) / f"v{version}.py"
            if frag_file.exists():
                self._nodes[nid] = NodeFragment(
                    node_id=nid,
                    kind=str(meta.get("kind", "unknown")),
                    source=frag_file.read_text("utf-8"),
                    version=version,
                )

    def _save_metadata(self) -> None:
        meta_path = self._root / "metadata.json"
        data = {str(k): v for k, v in self._metadata.items()}
        meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _write_fragment_to_disk(self, fragment: NodeFragment) -> None:
        frag_dir = self._fragment_dir(fragment.node_id)
        frag_dir.mkdir(parents=True, exist_ok=True)
        (frag_dir / f"v{fragment.version}.py").write_text(
            fragment.source, encoding="utf-8"
        )

    def _next_version(self, node_id: NodeId) -> int:
        committed = self._nodes.get(node_id)
        return committed.version + 1 if committed is not None else 1

    def _order(self, node_id: NodeId) -> int:
        value = self._metadata.get(node_id, {}).get("order", 0)
        return value if isinstance(value, int) else 0

    def get_node(self, node_id: NodeId, version: int | None = None) -> NodeFragment:
        """Return the latest committed fragment, or a specific prior version."""
        with self._lock:
            if node_id not in self._nodes:
                raise NodeStoreError(f"node not found: {node_id}")
            latest = self._nodes[node_id]
            if version is None or version == latest.version:
                return latest
            frag_file = self._fragment_dir(node_id) / f"v{version}.py"
            if not frag_file.exists():
                raise NodeStoreError(f"version {version} not found for node: {node_id}")
            return NodeFragment(
                node_id=node_id,
                kind=latest.kind,
                source=frag_file.read_text("utf-8"),
                version=version,
            )

    def put_node(self, node_id: NodeId, fragment: NodeFragment) -> NodeFragment:
        """Stage a new version of a node (uncommitted). Returns the versioned fragment.

        The store assigns the version (``current + 1``); the incoming fragment's
        ``version`` field is ignored so callers cannot collide or skip versions.
        """
        with self._lock:
            staged = dataclasses.replace(fragment, version=self._next_version(node_id))
            self._pending[node_id] = staged
            self._write_fragment_to_disk(staged)
            return staged

    def commit_node(self, node_id: NodeId) -> None:
        """Promote a pending fragment to committed.

        Committing a *whole-file* node (a bare ``path.py`` with no ``::kind::name``)
        supersedes that file's fragment nodes: the whole file now defines the file,
        so any previously-ingested ``path.py::…`` fragments are dropped. Otherwise
        reconstruction would concatenate the whole file *and* its old fragments and
        emit every top-level symbol twice.
        """
        with self._lock:
            if node_id not in self._pending:
                raise NodeStoreError(f"no pending version for node: {node_id}")
            fragment = self._pending.pop(node_id)
            self._nodes[node_id] = fragment
            existing = self._metadata.get(node_id, {})
            order = existing.get("order", len(self._metadata))
            self._metadata[node_id] = {
                "kind": fragment.kind,
                "version": fragment.version,
                "order": order,
            }
            if "::" not in str(node_id):
                self._supersede_fragments(str(node_id))
            self._save_metadata()

    def _supersede_fragments(self, file_path: str) -> None:
        """Drop a file's ``path::…`` fragments — a whole-file node now defines it."""
        prefix = f"{file_path}::"
        for nid in [n for n in self._nodes if str(n).startswith(prefix)]:
            del self._nodes[nid]
        for nid in [n for n in self._pending if str(n).startswith(prefix)]:
            del self._pending[nid]
        for nid in [n for n in self._metadata if str(n).startswith(prefix)]:
            del self._metadata[nid]

    def rollback_node(self, node_id: NodeId) -> None:
        """Discard a pending (uncommitted) fragment."""
        with self._lock:
            if node_id not in self._pending:
                return
            frag = self._pending.pop(node_id)
            frag_file = self._fragment_dir(node_id) / f"v{frag.version}.py"
            # Only delete the file if this version was never committed.
            committed = self._nodes.get(node_id)
            never_committed = committed is None or committed.version != frag.version
            if frag_file.exists() and never_committed:
                frag_file.unlink()

    def revert_node(self, node_id: NodeId) -> NodeFragment:
        """Roll a committed node back to its immediately previous version."""
        with self._lock:
            if node_id not in self._nodes:
                raise NodeStoreError(f"node not found: {node_id}")
            current = self._nodes[node_id]
            previous = self.get_node(node_id, version=current.version - 1)
            self._nodes[node_id] = previous
            self._metadata[node_id] = {
                **self._metadata.get(node_id, {}),
                "kind": previous.kind,
                "version": previous.version,
            }
            self._save_metadata()
            return previous

    def list_versions(self, node_id: NodeId) -> list[int]:
        """List all on-disk version numbers for a node, ascending."""
        with self._lock:
            frag_dir = self._fragment_dir(node_id)
            if not frag_dir.exists():
                return []
            versions = [
                int(p.stem[1:])
                for p in frag_dir.glob("v*.py")
                if p.stem[1:].isdigit()
            ]
            return sorted(versions)

    def list_nodes(self, file_path: str | None = None) -> list[NodeId]:
        """List committed node IDs in source order, optionally filtered by file.

        A *whole-file* node — a bare path id equal to ``file_path`` with no
        ``::kind::name`` suffix — is included for its own file. Such nodes are how a
        brand-new file is created: the agent returns the entire file as one node, and
        reconstruction writes it verbatim (a single fragment is already the file).
        """
        with self._lock:
            nodes = sorted(self._nodes, key=self._order)
            if file_path is None:
                return nodes
            prefix = f"{file_path}::"
            return [
                nid for nid in nodes
                if str(nid) == file_path or str(nid).startswith(prefix)
            ]

    def parse_file_into_nodes(
        self, file_path: str, source: str | None = None
    ) -> list[NodeId]:
        """Parse a file, store all fragments in order, and return node IDs."""
        with self._lock:
            fragments = parse_file_into_fragments(file_path, source)
            node_ids: list[NodeId] = []
            for order, frag in enumerate(fragments):
                self._pending[frag.node_id] = dataclasses.replace(frag, version=1)
                self._write_fragment_to_disk(self._pending[frag.node_id])
                self._metadata[frag.node_id] = {"order": order}
                self.commit_node(frag.node_id)
                node_ids.append(frag.node_id)
            return node_ids

    def get_committed_fragments(self, file_path: str) -> list[NodeFragment]:
        """Return all committed fragments for a file, in original source order."""
        with self._lock:
            return [self._nodes[nid] for nid in self.list_nodes(file_path)]

    def get_staged(self, node_id: NodeId) -> NodeFragment | None:
        """Return the pending (staged, uncommitted) fragment for a node, if any.

        Lets a caller inspect a new fragment version *before* committing it — e.g.
        the conflict detector, which must validate the proposed source before it is
        promoted during the collection phase.
        """
        with self._lock:
            return self._pending.get(node_id)
