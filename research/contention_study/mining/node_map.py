"""Map source line ranges onto MAK's own AST-node decomposition.

``mak.node_store.ingestion.parse_file_into_fragments`` returns fragments in
source order carrying their *raw* text but no line numbers, and it drops
whitespace-only gaps. This module re-attaches line spans by walking the
fragments against the original lines, so the study's node model is literally the
kernel's, not a reimplementation of it.

The kernel is imported read-only; nothing here writes to a node store.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from mak.node_store.ingestion import parse_file_into_fragments

from mining.exceptions import NodeMappingError

# A dropped whitespace-only gap is at most a handful of blank lines; a longer
# unexplained jump means the alignment assumption broke and we should say so.
_MAX_ALIGNMENT_SKIP = 4096

WHOLE_FILE_KIND = "file"


def whole_file_node_id(path: str) -> str:
    """Node id standing for a file MAK cannot decompose (non-Python, unparseable).

    MAK locks such a file as a unit, so the study models it as a single node
    rather than dropping it — see the non-Python accounting in the write-up.
    """
    return f"{path}::__file__::__whole__"


@dataclass(frozen=True, slots=True)
class NodeSpan:
    """One node and the 1-indexed, inclusive line range it occupies."""

    node_id: str
    kind: str
    start: int
    end: int

    def intersects(self, first: int, last: int) -> bool:
        """Whether this span overlaps the inclusive line range ``[first, last]``."""
        return not (last < self.start or first > self.end)


@dataclass(frozen=True, slots=True)
class FileNodeIndex:
    """All node spans of one file version, with a lookup by line."""

    path: str
    spans: tuple[NodeSpan, ...]
    parsed: bool

    def node_at(self, line: int) -> NodeSpan | None:
        """Return the node containing ``line``, or None for a dropped gap."""
        if not self.spans:
            return None
        starts = [span.start for span in self.spans]
        index = bisect_right(starts, line) - 1
        if index < 0:
            return None
        span = self.spans[index]
        return span if span.start <= line <= span.end else None

    def nodes_in(self, first: int, last: int) -> tuple[NodeSpan, ...]:
        """Every node intersecting the inclusive line range ``[first, last]``."""
        return tuple(span for span in self.spans if span.intersects(first, last))

    @property
    def node_ids(self) -> frozenset[str]:
        """Distinct node ids in this file version."""
        return frozenset(span.node_id for span in self.spans)


def _align(
    lines: list[str], fragment_lines: list[str], cursor: int, path: str
) -> int:
    """Return where ``fragment_lines`` starts in ``lines``, at or after ``cursor``."""
    count = len(fragment_lines)
    limit = min(len(lines) - count, cursor + _MAX_ALIGNMENT_SKIP)
    for offset in range(cursor, limit + 1):
        if lines[offset : offset + count] == fragment_lines:
            return offset
    raise NodeMappingError(
        f"could not align fragment of {count} lines in {path} "
        f"at or after line {cursor + 1}"
    )


def index_file(path: str, source: str) -> FileNodeIndex:
    """Decompose one Python source into node spans.

    A file the kernel cannot parse is represented by a single whole-file node,
    with ``parsed=False`` so the caller can count parse failures honestly rather
    than treating the file as node-free.
    """
    try:
        fragments = parse_file_into_fragments(path, source)
    except (SyntaxError, ValueError, RecursionError):
        return _unparsed_index(path, source)

    lines = source.splitlines(keepends=True)
    spans: list[NodeSpan] = []
    cursor = 0
    for fragment in fragments:
        fragment_lines = fragment.source.splitlines(keepends=True)
        if not fragment_lines:
            continue
        try:
            offset = _align(lines, fragment_lines, cursor, path)
        except NodeMappingError:
            return _unparsed_index(path, source)
        spans.append(
            NodeSpan(
                node_id=str(fragment.node_id),
                kind=fragment.kind,
                start=offset + 1,
                end=offset + len(fragment_lines),
            )
        )
        cursor = offset + len(fragment_lines)
    return FileNodeIndex(path=path, spans=tuple(spans), parsed=True)


def _unparsed_index(path: str, source: str) -> FileNodeIndex:
    """Fallback index treating the whole file as one lockable unit."""
    total = max(1, len(source.splitlines()))
    span = NodeSpan(
        node_id=whole_file_node_id(path), kind=WHOLE_FILE_KIND, start=1, end=total
    )
    return FileNodeIndex(path=path, spans=(span,), parsed=False)


def empty_index(path: str) -> FileNodeIndex:
    """Index for a file version that does not exist (added or deleted file)."""
    return FileNodeIndex(path=path, spans=(), parsed=True)
