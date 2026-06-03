"""Split Python files into ordered, raw-source AST node fragments.

The ingestion layer tiles a source file into a sequence of fragments that cover
every line exactly once, in original order. Fragments retain their *raw* source
text — decorators, inline comments, and surrounding standalone comments included
— so that reconstruction is a position-preserving concatenation rather than an
``ast.unparse()`` re-render (which would silently drop comments and decorators).

Fragment kinds:

- ``module_header``  — leading top-level statements before the first def/class
- ``function``       — a top-level ``def`` / ``async def`` (decorators included)
- ``class``          — a class *shell*: the ``class`` line plus members up to the
                       first method (decorators, docstring, leading attributes)
- ``method``         — a method defined directly inside a top-level class,
                       qualified as ``Class.method``
- ``class_body``     — class-level statements that follow a method
- ``module_body``    — top-level statements that follow the first def/class
"""

from __future__ import annotations

import ast
import fnmatch
from dataclasses import dataclass
from pathlib import Path

from mak.core.types import NodeFragment, NodeId

_FuncDef = (ast.FunctionDef, ast.AsyncFunctionDef)


def _node_id(file_path: str, kind: str, name: str) -> NodeId:
    return NodeId(f"{file_path}::{kind}::{name}")


def _unique_id(
    file_path: str, kind: str, name: str, used: set[str]
) -> NodeId:
    """Return a collision-free node id, suffixing ``#n`` on duplicates.

    Handles ``@overload`` stubs and conditionally-defined same-name symbols,
    which would otherwise share an id and silently overwrite one another.
    """
    candidate = _node_id(file_path, kind, name)
    if candidate not in used:
        used.add(candidate)
        return candidate
    counter = 2
    while True:
        suffixed = _node_id(file_path, kind, f"{name}#{counter}")
        if suffixed not in used:
            used.add(suffixed)
            return suffixed
        counter += 1


@dataclass(frozen=True, slots=True)
class _Span:
    """A splittable top-level item and the lines it occupies (0-indexed)."""

    start: int  # inclusive, includes decorator lines
    end: int  # exclusive
    kind: str
    name: str
    node: ast.AST


def _item_start(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> int:
    """Return the 0-indexed first line of a node, decorators included."""
    if node.decorator_list:
        return min(d.lineno for d in node.decorator_list) - 1
    return node.lineno - 1


def _splittable_spans(
    body: list[ast.stmt], func_kind: str, name_prefix: str = ""
) -> list[_Span]:
    """Collect def/class statements from a block, in source order."""
    spans: list[_Span] = []
    for node in body:
        if isinstance(node, _FuncDef):
            name = f"{name_prefix}{node.name}"
            end = node.end_lineno or 0
            spans.append(_Span(_item_start(node), end, func_kind, name, node))
        elif isinstance(node, ast.ClassDef):
            end = node.end_lineno or 0
            spans.append(_Span(_item_start(node), end, "class", node.name, node))
    return spans


def _gap_fragment(
    file_path: str,
    lines: list[str],
    start: int,
    end: int,
    seen_item: bool,
    gap_kinds: tuple[str, str],
    gap_name: tuple[str, str],
    used: set[str],
) -> NodeFragment | None:
    """Build a fragment for an interstitial (non-def/class) line range.

    Whitespace-only gaps are dropped — they carry no comments or code, and
    ``ruff format`` re-establishes blank-line spacing on reconstruction.
    """
    text = "".join(lines[start:end])
    if not text.strip():
        return None
    kind = gap_kinds[1] if seen_item else gap_kinds[0]
    name = gap_name[1] if seen_item else gap_name[0]
    node_id = _unique_id(file_path, kind, name, used)
    return NodeFragment(node_id=node_id, kind=kind, source=text, version=1)


def _tile(
    file_path: str,
    lines: list[str],
    region_start: int,
    region_end: int,
    spans: list[_Span],
    gap_kinds: tuple[str, str],
    gap_name: tuple[str, str],
    used: set[str],
) -> list[NodeFragment]:
    """Tile a line region into gap + item fragments, in order, covering all lines."""
    fragments: list[NodeFragment] = []
    cursor = region_start
    seen_item = False

    for span in spans:
        if span.start > cursor:
            gap = _gap_fragment(
                file_path, lines, cursor, span.start,
                seen_item, gap_kinds, gap_name, used,
            )
            if gap is not None:
                fragments.append(gap)
        fragments.extend(_item_fragments(file_path, lines, span, used))
        cursor = span.end
        seen_item = True

    if cursor < region_end:
        gap = _gap_fragment(
            file_path, lines, cursor, region_end,
            seen_item, gap_kinds, gap_name, used,
        )
        if gap is not None:
            fragments.append(gap)
    return fragments


def _item_fragments(
    file_path: str, lines: list[str], span: _Span, used: set[str]
) -> list[NodeFragment]:
    """Build fragment(s) for one def/class span (classes split into method nodes)."""
    if span.kind == "class" and isinstance(span.node, ast.ClassDef):
        return _tile(
            file_path,
            lines,
            span.start,
            span.end,
            _splittable_spans(span.node.body, "method", f"{span.name}."),
            gap_kinds=("class", "class_body"),
            gap_name=(span.name, span.name),
            used=used,
        )
    node_id = _unique_id(file_path, span.kind, span.name, used)
    source = "".join(lines[span.start : span.end])
    return [NodeFragment(node_id=node_id, kind=span.kind, source=source, version=1)]


def parse_file_into_fragments(
    file_path: str,
    source: str | None = None,
) -> list[NodeFragment]:
    """Parse a Python file into ordered, raw-source node fragments.

    The returned list is in source order; reconstruction preserves that order,
    so decorators, comments, and statement ordering survive a round trip.
    """
    path = Path(file_path)
    if source is None:
        source = path.read_text(encoding="utf-8")

    tree = ast.parse(source, filename=file_path)
    lines = source.splitlines(keepends=True)
    used: set[str] = set()

    return _tile(
        file_path,
        lines,
        region_start=0,
        region_end=len(lines),
        spans=_splittable_spans(tree.body, "function"),
        gap_kinds=("module_header", "module_body"),
        gap_name=("__header__", "__body__"),
        used=used,
    )


def _is_excluded(rel: str, exclude_patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatch(rel, ep)
        or (ep.startswith("**/") and fnmatch.fnmatch(rel, ep[3:]))
        for ep in exclude_patterns
    )


def walk_and_parse(
    root: Path,
    include_patterns: tuple[str, ...] = ("**/*.py",),
    exclude_patterns: tuple[str, ...] = (),
) -> dict[str, list[NodeFragment]]:
    """Walk a directory tree and parse all matching Python files."""
    result: dict[str, list[NodeFragment]] = {}

    for pattern in include_patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel = str(path.relative_to(root))
            if _is_excluded(rel, exclude_patterns) or rel in result:
                continue
            try:
                result[rel] = parse_file_into_fragments(rel, path.read_text("utf-8"))
            except SyntaxError:
                continue

    return result
