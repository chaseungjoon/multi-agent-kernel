"""A hierarchical view of the node inventory, for planner prompts.

The planner used to be shown every node id, every time: MAK's own tree is ~26 K
tokens of ids alone, and a large repository several hundred thousand. This view
renders the same inventory at three resolutions, so a prompt can show as much as
a budget allows and say exactly what it left out:

- **level 0 — tree.** Directories and files with node counts. A directory that
  does not fit the tree's share of the budget renders as one collapsed line.
- **level 1 — file detail.** A header, then one line per node: its id *suffix*
  (the full id is the file path plus the suffix), its **shape** (the ``def`` or
  ``class`` line, defaults elided — see
  :func:`~mak.node_store.api_digest.node_signature`) and its **incoming
  references** from the dependency graph, so the model reads real callers
  instead of guessing them from names. A file larger than a cap shows its first
  nodes and one line counting the rest.
- **flat** — the ``  - <id>`` listing the ``oneshot`` strategy has always sent,
  kept byte-identical.

Every rendering is deterministic — sorted, no sets in the output, no
timestamps, no absolute paths — because the prompt prefix built from it is what
a provider caches.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from mak.agent_runner.adapters.ollama_api_adapter import estimate_tokens
from mak.core.types import NodeId
from mak.node_store.api_digest import node_signature
from mak.planner.depgraph import DepGraph
from mak.planner.depgraph import referrers as invert_references

# Tokens are estimated as ``len / 4`` (``estimate_tokens``), so a token cap is
# this many characters.
CHARS_PER_TOKEN = 4
COLLAPSED_SUFFIX = " [collapsed — expand to see]"
_WHOLE_FILE_LINE = "  (whole-file node: target the file path itself)"
_TOP_REF_FILES = 3


def _plural(count: int, noun: str) -> str:
    """Return ``"1 file"`` / ``"1,502 files"``."""
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _file_of(node_id: str) -> str:
    return node_id.split("::", 1)[0]


def _parent_dir(path: str) -> str:
    """Return ``path``'s directory key (``a/b/c.py`` → ``a/b/``; root is ``""``)."""
    head, sep, _tail = path.rstrip("/").rpartition("/")
    return f"{head}{sep}" if sep else ""


def _name(path: str) -> str:
    """Return the last segment of a file path or directory key."""
    return path.rstrip("/").rpartition("/")[2]


@dataclass(frozen=True, slots=True)
class NodeLine:
    """One node's level-1 entry: id suffix, shape, and who references it."""

    node_id: NodeId
    suffix: str
    kind: str
    name: str
    shape: str | None
    ref_count: int
    # (file, reference count), most-referencing first, then by path.
    ref_files: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class FileEntry:
    """A file's nodes, in source order, and how many other files use it."""

    path: str
    nodes: tuple[NodeLine, ...]
    used_by: int


@dataclass(frozen=True, slots=True)
class RenderedFile:
    """A level-1 rendering and how many nodes the cap left out."""

    text: str
    nodes_shown: int
    nodes_hidden: int


@dataclass(frozen=True, slots=True)
class RenderedTree:
    """A level-0 rendering: its text, open directories, and what stayed shut."""

    text: str
    expanded: frozenset[str]
    collapsed: int
    omitted: int = 0


@dataclass
class _Dir:
    """A directory key's direct children and subtree totals."""

    subdirs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    file_total: int = 0
    node_total: int = 0


class InventoryView:
    """The inventory rendered at tree, file-detail and flat resolutions.

    Built once per store generation (the session caches it) from the node ids in
    inventory order, their sources (for shapes) and the dependency graph (for
    references). Without sources or a graph the view still works — shapes and
    reference columns are simply absent.
    """

    def __init__(
        self,
        inventory: Sequence[NodeId],
        *,
        sources: Mapping[NodeId, str] | None = None,
        graph: DepGraph | None = None,
        referrers: Mapping[NodeId, frozenset[NodeId]] | None = None,
    ) -> None:
        self._inventory = list(inventory)
        self._sources: Mapping[NodeId, str] = sources or {}
        self._references = dict(graph.references) if graph is not None else {}
        if referrers is None:
            referrers = invert_references(graph) if graph is not None else {}
        self._referrers = referrers
        grouped: dict[str, list[NodeId]] = {}
        for node_id in self._inventory:
            grouped.setdefault(_file_of(str(node_id)), []).append(node_id)
        self._grouped = {path: grouped[path] for path in sorted(grouped)}
        self._dirs = self._build_dirs()
        # File entries parse their nodes' shapes, so they are built on first
        # use: a session that only validates a plan never renders one.
        self._entries: dict[str, FileEntry] = {}
        self._file_text: dict[tuple[str, int | None], RenderedFile] = {}
        self._full: str | None = None

    # -- construction -----------------------------------------------------

    def _build_entry(self, path: str) -> FileEntry:
        ids = self._grouped[path]
        lines = tuple(self._node_line(nid, self._sources.get(nid)) for nid in ids)
        users = {
            _file_of(str(r))
            for nid in ids
            for r in self._referrers.get(nid, frozenset())
        }
        users.discard(path)
        return FileEntry(path=path, nodes=lines, used_by=len(users))

    def _node_line(self, node_id: NodeId, source: str | None) -> NodeLine:
        text = str(node_id)
        _path, sep, rest = text.partition("::")
        kind, _, name = rest.partition("::") if sep else ("", "", "")
        callers = self._referrers.get(node_id, frozenset())
        per_file: dict[str, int] = {}
        for caller in callers:
            caller_file = _file_of(str(caller))
            per_file[caller_file] = per_file.get(caller_file, 0) + 1
        ranked = tuple(
            sorted(per_file.items(), key=lambda item: (-item[1], item[0]))
        )
        return NodeLine(
            node_id=node_id,
            suffix=f"::{rest}" if sep else "",
            kind=kind,
            name=name,
            shape=node_signature(source, kind) if source is not None else None,
            ref_count=len(callers),
            ref_files=ranked,
        )

    def _build_dirs(self) -> dict[str, _Dir]:
        dirs: dict[str, _Dir] = {"": _Dir()}
        for path, ids in self._grouped.items():
            parent = _parent_dir(path)
            self._ensure_dir(dirs, parent)
            dirs[parent].files.append(path)
            key: str | None = parent
            while key is not None:
                dirs[key].file_total += 1
                dirs[key].node_total += len(ids)
                key = None if key == "" else _parent_dir(key)
        for node in dirs.values():
            node.subdirs.sort()
            node.files.sort()
        return dirs

    def _ensure_dir(self, dirs: dict[str, _Dir], key: str) -> None:
        if key in dirs:
            return
        dirs[key] = _Dir()
        parent = _parent_dir(key)
        self._ensure_dir(dirs, parent)
        dirs[parent].subdirs.append(key)

    # -- lookups ----------------------------------------------------------

    @property
    def inventory(self) -> list[NodeId]:
        """The node ids the view was built from, in inventory order."""
        return list(self._inventory)

    @property
    def files(self) -> tuple[str, ...]:
        """Every file path, sorted."""
        return tuple(self._grouped)

    @property
    def node_count(self) -> int:
        """How many nodes the inventory holds."""
        return len(self._inventory)

    def file(self, path: str) -> FileEntry | None:
        """Return a file's entry, or None when it has no nodes."""
        if path not in self._grouped:
            return None
        entry = self._entries.get(path)
        if entry is None:
            entry = self._entries[path] = self._build_entry(path)
        return entry

    def dir_key(self, path: str) -> str | None:
        """Return the directory key for ``path`` (trailing ``/`` optional)."""
        key = path.strip().strip("/")
        key = f"{key}/" if key else ""
        return key if key in self._dirs and key else None

    def neighbour_files(self, path: str) -> frozenset[str]:
        """Files holding a referrer or a referent of any node in ``path``."""
        found: set[str] = set()
        for node_id in self._grouped.get(path, ()):
            for other in self._referrers.get(node_id, frozenset()):
                found.add(_file_of(str(other)))
            for other in self._references.get(node_id, frozenset()):
                found.add(_file_of(str(other)))
        found.discard(path)
        return frozenset(found)

    def close_files(self, path: str, limit: int = 3) -> list[str]:
        """Return up to ``limit`` known file paths close to ``path``."""
        return difflib.get_close_matches(
            path, list(self._grouped), n=limit, cutoff=0.5
        )

    # -- flat and level 1 -------------------------------------------------

    def render_flat(self) -> str:
        """Return today's ``  - <id>`` listing (the ``oneshot`` inventory)."""
        return "\n".join(f"  - {nid}" for nid in self._inventory) or "  (empty)"

    def render_file(self, path: str, max_tokens: int | None = None) -> RenderedFile:
        """Return ``path``'s level-1 detail, cut to ``max_tokens`` if it is larger."""
        key = (path, max_tokens)
        cached = self._file_text.get(key)
        if cached is None:
            entry = self.file(path)
            if entry is None:
                raise KeyError(f"no file {path!r} in the inventory")
            cached = self._render_file(entry, max_tokens)
            self._file_text[key] = cached
        return cached

    def file_tokens(self, path: str, max_tokens: int | None = None) -> int:
        """Estimated tokens of ``render_file(path, max_tokens)``."""
        return estimate_tokens(self.render_file(path, max_tokens).text)

    def _render_file(self, entry: FileEntry, max_tokens: int | None) -> RenderedFile:
        header = f"{entry.path}  ({_plural(len(entry.nodes), 'node')}"
        if entry.used_by:
            header += f" · used by {_plural(entry.used_by, 'other file')}"
        header += ")"
        lines = [self._line(entry.path, node) for node in entry.nodes]
        full = "\n".join([header, *lines])
        if max_tokens is None or estimate_tokens(full) <= max_tokens:
            return RenderedFile(full, len(lines), 0)
        budget = max_tokens * CHARS_PER_TOKEN
        # size(k) = header + the first k lines + the "k more" line, newlines
        # included; the largest k that fits wins (at least the header is kept).
        size = len(header)
        shown = 0
        for line in lines:
            tail = len(_hidden_line(len(lines) - shown - 1)) + 1
            if size + len(line) + 1 + tail > budget:
                break
            size += len(line) + 1
            shown += 1
        hidden = len(lines) - shown
        text = "\n".join([header, *lines[:shown], _hidden_line(hidden)])
        return RenderedFile(text, shown, hidden)

    def _line(self, path: str, node: NodeLine) -> str:
        if not node.suffix:
            return _WHOLE_FILE_LINE
        parts = [f"  {node.suffix}"]
        if node.shape:
            parts.append(node.shape)
        if node.ref_count:
            parts.append(_refs_text(path, node))
        return "  ".join(parts)

    def render_full(self) -> str:
        """Return level-1 detail for every file (the ``full`` inventory)."""
        if self._full is None:
            blocks = [self.render_file(path).text for path in self._grouped]
            self._full = "\n".join(blocks) or "  (empty)"
        return self._full

    def full_tokens(self) -> int:
        """Estimated tokens of :meth:`render_full`."""
        return estimate_tokens(self.render_full())

    # -- level 0 ----------------------------------------------------------

    def render_tree(self, max_tokens: int) -> RenderedTree:
        """Return the tree, opening directories breadth-first while it fits.

        Directories open in path order, level by level; one that does not fit
        stays collapsed and the rest are still tried. If even the top level does
        not fit, it is cut and the rest counted in ``omitted``.
        """
        budget = max_tokens * CHARS_PER_TOKEN
        expanded = {""}
        size = sum(len(line) + 1 for line in self._child_lines("", expanded, 0))
        if size > budget:
            return self._truncated_root(budget)
        queue = list(self._dirs[""].subdirs)
        while queue:
            key = queue.pop(0)
            depth = key.count("/")
            opened = self._child_lines(key, expanded | {key}, depth)
            delta = sum(len(line) + 1 for line in opened) - len(COLLAPSED_SUFFIX)
            if size + delta <= budget:
                expanded.add(key)
                size += delta
                queue.extend(self._dirs[key].subdirs)
        lines = self._tree_lines("", frozenset(expanded), 0)
        collapsed = sum(1 for line in lines if line.endswith(COLLAPSED_SUFFIX))
        return RenderedTree("\n".join(lines), frozenset(expanded), collapsed)

    def _truncated_root(self, budget: int) -> RenderedTree:
        lines = self._child_lines("", {""}, 0)
        kept: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > budget - 80:
                break
            kept.append(line)
            size += len(line) + 1
        omitted = len(lines) - len(kept)
        kept.append(f"… {omitted:,} more top-level entries not shown (budget)")
        collapsed = sum(1 for line in kept if line.endswith(COLLAPSED_SUFFIX))
        return RenderedTree("\n".join(kept), frozenset({""}), collapsed, omitted)

    def collapsed_count(self, expanded: frozenset[str]) -> int:
        """Directories visible under ``expanded`` but not open themselves."""
        return sum(
            1 for key in self._dirs
            if key and key not in expanded and _parent_dir(key) in expanded
        )

    def render_dir(
        self, key: str, expanded: frozenset[str]
    ) -> tuple[str, frozenset[str]] | None:
        """Show directory ``key`` one level deeper than ``expanded`` shows it.

        A collapsed directory opens itself; an open one opens its collapsed
        subdirectories. Returns the rendering and the new open set, or None when
        everything under ``key`` is already shown.
        """
        if key not in expanded:
            frontier = [key]
        else:
            frontier = [
                sub for sub in self._subtree(key)
                if sub not in expanded and _parent_dir(sub) in expanded
            ]
        if not frontier:
            return None
        opened = expanded | frozenset(frontier)
        blocks: list[str] = []
        for sub in sorted(frontier):
            node = self._dirs[sub]
            header = (
                f"{sub} ({_plural(node.file_total, 'file')}, "
                f"{_plural(node.node_total, 'node')})"
            )
            blocks.append("\n".join([header, *self._child_lines(sub, opened, 1)]))
        return "\n".join(blocks), opened

    def _subtree(self, key: str) -> list[str]:
        """Every directory strictly below ``key``, breadth-first."""
        found: list[str] = []
        queue = list(self._dirs[key].subdirs)
        while queue:
            sub = queue.pop(0)
            found.append(sub)
            queue.extend(self._dirs[sub].subdirs)
        return found

    def _children(self, key: str) -> list[str]:
        node = self._dirs[key]
        return sorted([*node.subdirs, *node.files], key=_name)

    def _child_lines(
        self, key: str, expanded: set[str] | frozenset[str], depth: int
    ) -> list[str]:
        """Return the lines for ``key``'s direct children (subdirectories shut)."""
        return [
            self._entry_line(child, expanded, depth) for child in self._children(key)
        ]

    def _entry_line(
        self, child: str, expanded: set[str] | frozenset[str], depth: int
    ) -> str:
        indent = "  " * depth
        if child.endswith("/"):
            node = self._dirs[child]
            line = (
                f"{indent}{_name(child)}/ ({_plural(node.file_total, 'file')}, "
                f"{_plural(node.node_total, 'node')})"
            )
            return line if child in expanded else line + COLLAPSED_SUFFIX
        return f"{indent}{_name(child)} {len(self._grouped[child]):,}"

    def _tree_lines(self, key: str, expanded: frozenset[str], depth: int) -> list[str]:
        lines: list[str] = []
        for child in self._children(key):
            lines.append(self._entry_line(child, expanded, depth))
            if child.endswith("/") and child in expanded:
                lines.extend(self._tree_lines(child, expanded, depth + 1))
        return lines


def _hidden_line(hidden: int) -> str:
    return (
        f"  … {_plural(hidden, 'more node')} not shown (budget); "
        "their ids are still valid targets"
    )


def _refs_text(path: str, node: NodeLine) -> str:
    """Render ``← n refs · m files (top files)``, or ``· this file`` when local."""
    head = f"← {_plural(node.ref_count, 'ref')}"
    if all(file_path == path for file_path, _ in node.ref_files):
        return f"{head} · this file"
    names = [
        "this file" if file_path == path else file_path
        for file_path, _ in node.ref_files[:_TOP_REF_FILES]
    ]
    extra = len(node.ref_files) - _TOP_REF_FILES
    if extra > 0:
        names.append(f"+{extra}")
    return f"{head} · {_plural(len(node.ref_files), 'file')} ({', '.join(names)})"
