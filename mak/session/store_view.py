"""Read-side helpers over the node store, shared by every session collaborator."""

from __future__ import annotations

from mak.core.exceptions import NodeStoreError
from mak.core.types import NodeFragment, NodeId
from mak.node_store.reconstruction import assemble_fragments
from mak.node_store.store import NodeStore


def file_of(node_id: str) -> str:
    """Return the file path component of a ``file::kind::name`` node id."""
    return node_id.split("::", 1)[0]


def is_header_id(node_id: str) -> bool:
    """Whether a node id refers to a ``module_header`` fragment."""
    parts = node_id.split("::")
    return len(parts) >= 2 and parts[1] == "module_header"


class StoreView:
    """Committed, staged and prospective sources, read without mutating the store."""

    def __init__(self, store: NodeStore) -> None:
        self.store = store

    def source(self, node_id: NodeId) -> str | None:
        """Return a node's current committed source, or None if it does not exist."""
        try:
            return self.store.get_node(node_id).source
        except NodeStoreError:
            return None

    def committed(self, node_id: NodeId) -> NodeFragment | None:
        """Return a node's committed fragment, or None when it has none."""
        try:
            return self.store.get_node(node_id)
        except NodeStoreError:
            return None

    def kind(self, node_id: NodeId) -> str:
        """Return a node's stored kind, inferring a sensible kind for a new node.

        A bare-path ``.py`` id (no ``::kind::name``) is a *whole-file* node — the
        agent returned an entire new file as one node — so its kind is ``module``;
        any other new id defaults to ``function``.
        """
        try:
            return self.store.get_node(node_id).kind
        except NodeStoreError:
            return "module" if "::" not in str(node_id) else "function"

    def file_fragment_ids(self, node_id: NodeId) -> list[NodeId]:
        """Return the committed fragments of a bare whole-file id (else none)."""
        if "::" in str(node_id):
            return []
        return [n for n in self.store.list_nodes(str(node_id)) if n != node_id]

    def dependency_source(self, node_id: NodeId) -> str | None:
        """Return a node's source, assembling a whole file from its fragments.

        A whole-file target is often committed as fragments rather than as one
        bare-path node, so the bare id itself has no source of its own.
        """
        source = self.source(node_id)
        if source is not None:
            return source
        if "::" in str(node_id):
            return None
        fragments = self.store.get_committed_fragments(str(node_id))
        return assemble_fragments(fragments) if fragments else None

    def file_source_or_empty(self, file_path: str) -> str:
        """Return a file's assembled committed source, or ``""`` when it has none."""
        fragments = self.store.get_committed_fragments(file_path)
        return assemble_fragments(fragments) if fragments else ""

    def staged_sources(self, staged: list[NodeId]) -> dict[NodeId, str]:
        """Return the pending sources of ``staged``, keyed by node id."""
        own: dict[NodeId, str] = {}
        for node_id in staged:
            fragment = self.store.get_staged(node_id)
            if fragment is not None:
                own[node_id] = fragment.source
        return own

    def preview(self, file_path: str, staged_set: set[NodeId]) -> str:
        """Build a file's prospective source: committed fragments + staged swaps.

        Delegates to ``NodeStore.get_preview_fragments`` so that fragments are
        re-indented (class methods back to column 4, etc.) before assembly —
        the same transformation ``get_committed_fragments`` applies during real
        reconstruction.  Using dedented ``get_node()`` sources here would make
        any file with class methods fail ``ast.parse`` unconditionally.
        """
        staged_overrides = {
            node_id: frag
            for node_id in staged_set
            if (frag := self.store.get_staged(node_id)) is not None
        }
        return assemble_fragments(
            self.store.get_preview_fragments(file_path, staged_overrides)
        )

    def file_compiles(self, node_id: NodeId) -> bool:
        """Return True if the committed file containing this node parses as Python.

        Guards the no-op acceptance path: a task whose agent returned success
        with no changes must not be accepted as complete when the file it was
        supposed to fix still has a syntax error.
        """
        try:
            compile(self.preview(file_of(str(node_id)), set()), "<mak>", "exec")
            return True
        except SyntaxError:
            return False
