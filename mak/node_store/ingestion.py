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
import re
from collections.abc import Callable
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


def _dir_is_excluded(rel: str, exclude_patterns: tuple[str, ...]) -> bool:
    """Whether *nothing* under a directory can be included, so skip descending.

    A pattern excludes a whole subtree when it ends in ``/**`` — ``**/.venv/**``
    is exactly "everything under any .venv" — so matching that prefix against the
    directory itself answers the question without walking it. Patterns that do
    not end in ``/**`` only ever exclude individual files and are left to
    :func:`_is_excluded`, so a pruned directory is always one whose every
    descendant the per-file check would have rejected anyway.
    """
    for pattern in exclude_patterns:
        if not pattern.endswith("/**"):
            continue
        stem = pattern[:-3]
        if fnmatch.fnmatch(rel, stem) or (
            stem.startswith("**/") and fnmatch.fnmatch(rel, stem[3:])
        ):
            return True
    return False


def _segment_regex(segment: str) -> str:
    """Translate one glob path segment to regex source that cannot cross ``/``.

    ``fnmatch.translate`` is unusable here: its ``*`` becomes ``.*``, which
    happily spans separators, so ``src/*.py`` would match ``src/deep/x.py``.
    """
    out: list[str] = []
    i = 0
    while i < len(segment):
        char = segment[i]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = _closing_bracket(segment, i)
            if close is None:
                out.append(re.escape(char))
            else:
                body = segment[i + 1 : close]
                if body.startswith(("!", "^")):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = close + 1
                continue
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def _closing_bracket(segment: str, open_at: int) -> int | None:
    """Index of the ``]`` closing a character class, or None if unterminated."""
    index = open_at + 1
    if index < len(segment) and segment[index] in "!^":
        index += 1
    if index < len(segment) and segment[index] == "]":
        index += 1  # a leading ']' is a literal member, not the terminator
    while index < len(segment) and segment[index] != "]":
        index += 1
    return index if index < len(segment) else None


# A pattern that matches no file at all. ``Path.glob`` resolves a *trailing*
# bare ``**`` to directories only, so an include pattern ending in one ingests
# nothing; reproducing that exactly keeps the pruning walk a pure performance
# change rather than a quiet widening of what gets ingested.
_MATCH_NOTHING = re.compile(r"(?!)")


def _include_regex(pattern: str) -> re.Pattern[str]:
    """Compile a ``Path.glob`` include pattern into a matcher for relative paths.

    ``**`` followed by a separator matches zero or more whole path segments,
    which is what makes ``**/*.py`` match a file at the root as well as one
    nested ten deep. Every other wildcard is confined to a single segment.
    """
    parts = pattern.split("/")
    out: list[str] = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            if last:
                return _MATCH_NOTHING
            out.append("(?:[^/]+/)*")
            continue
        out.append(_segment_regex(part))
        if not last:
            out.append("/")
    return re.compile("".join(out) + r"\Z")


def iter_source_files(
    root: Path,
    include_patterns: tuple[str, ...] = ("**/*.py",),
    exclude_patterns: tuple[str, ...] = (),
    *,
    skip: Callable[[Path], bool] | None = None,
    ignore: Callable[[str, bool], bool] | None = None,
) -> list[Path]:
    """Return the files an ingest should read, without walking what it discards.

    Both callers used to ``glob("**/*.py")`` the whole tree and *then* drop the
    excluded paths. The exclusion was correct; the walk was not — it descended
    into ``.venv``, ``node_modules``, ``site-packages`` and ``__pycache__``,
    which on a repo with a populated virtualenv is the slowest part of
    ``Session.initialize()`` and finds tens of thousands of files it will
    immediately throw away.

    An excluded *directory* is pruned before it is entered; everything else —
    which patterns match, the per-file exclusion check, the order files come back
    in, and the refusal to follow a symlinked directory (``Path.glob``'s ``**``
    does not either) — is preserved, so the returned list is what the
    glob-then-filter produced. ``skip`` is an extra, non-negotiable predicate for
    paths that are never project source whatever the patterns say (the session's
    own ``.mak`` store). ``ignore`` is the project's ``.makignore``: called with
    the work-dir-relative path and whether it is a directory, and an ignored
    directory is pruned like an excluded one.
    """
    matchers = [(pattern, _include_regex(pattern)) for pattern in include_patterns]
    matched: dict[str, list[Path]] = {pattern: [] for pattern in include_patterns}
    stack = [root]
    while stack:
        for entry in _entries(stack.pop()):
            try:
                rel = entry.relative_to(root).as_posix()
            except ValueError:  # pragma: no cover - iterdir stays under root
                continue
            if skip is not None and skip(entry):
                continue
            if entry.is_dir():
                if (
                    not entry.is_symlink()
                    and not _dir_is_excluded(rel, exclude_patterns)
                    and not (ignore is not None and ignore(rel, True))
                ):
                    stack.append(entry)
                continue
            if not entry.is_file() or _is_excluded(rel, exclude_patterns):
                continue
            if ignore is not None and ignore(rel, False):
                continue
            for pattern, regex in matchers:
                if regex.match(rel):
                    matched[pattern].append(entry)
    # Per pattern, in the order the patterns were configured, sorted within each
    # — exactly the sequence ``for pattern in include_patterns: sorted(glob(…))``
    # produced. Later patterns never re-yield an earlier pattern's file.
    seen: set[Path] = set()
    ordered: list[Path] = []
    for pattern in include_patterns:
        for path in sorted(matched[pattern]):
            if path not in seen:
                seen.add(path)
                ordered.append(path)
    return ordered


def _entries(directory: Path) -> list[Path]:
    """List a directory's entries, treating an unreadable one as empty."""
    try:
        return list(directory.iterdir())
    except OSError:
        return []


def walk_and_parse(
    root: Path,
    include_patterns: tuple[str, ...] = ("**/*.py",),
    exclude_patterns: tuple[str, ...] = (),
    *,
    ignore: Callable[[str, bool], bool] | None = None,
) -> dict[str, list[NodeFragment]]:
    """Walk a directory tree and parse all matching Python files."""
    result: dict[str, list[NodeFragment]] = {}

    for path in iter_source_files(
        root, include_patterns, exclude_patterns, ignore=ignore
    ):
        rel = str(path.relative_to(root))
        if rel in result:
            continue
        try:
            result[rel] = parse_file_into_fragments(rel, path.read_text("utf-8"))
        except (SyntaxError, OSError):
            continue

    return result
