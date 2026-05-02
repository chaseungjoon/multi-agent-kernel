"""Walk Python files and extract AST node fragments."""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path

from mak.core.types import NodeFragment, NodeId


def _node_id(file_path: str, kind: str, name: str) -> NodeId:
    return NodeId(f"{file_path}::{kind}::{name}")


def _extract_source_segment(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    if segment is not None:
        return segment
    lines = source.splitlines(keepends=True)
    start = getattr(node, "lineno", 1) - 1
    end = getattr(node, "end_lineno", start + 1)
    return "".join(lines[start:end])


def parse_file_into_fragments(
    file_path: str,
    source: str | None = None,
) -> list[NodeFragment]:
    """Parse a Python file and return its AST node fragments."""
    path = Path(file_path)
    if source is None:
        source = path.read_text(encoding="utf-8")

    tree = ast.parse(source, filename=file_path)

    fragments: list[NodeFragment] = []
    header_lines: list[str] = []
    body_lines: list[str] = []
    all_lines = source.splitlines(keepends=True)

    top_level_ranges: list[tuple[int, int]] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno - 1
            end = node.end_lineno or (start + 1)
            top_level_ranges.append((start, end))
            segment = _extract_source_segment(source, node)
            frag = NodeFragment(
                node_id=_node_id(file_path, "function", node.name),
                kind="function",
                source=segment,
                version=1,
            )
            fragments.append(frag)

        elif isinstance(node, ast.ClassDef):
            start = node.lineno - 1
            end = node.end_lineno or (start + 1)
            top_level_ranges.append((start, end))
            segment = _extract_source_segment(source, node)
            frag = NodeFragment(
                node_id=_node_id(file_path, "class", node.name),
                kind="class",
                source=segment,
                version=1,
            )
            fragments.append(frag)

        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            start = node.lineno - 1
            end = node.end_lineno or (start + 1)
            top_level_ranges.append((start, end))
            header_lines.extend(all_lines[start:end])

        else:
            start = node.lineno - 1
            end = node.end_lineno or (start + 1)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                top_level_ranges.append((start, end))
                header_lines.extend(all_lines[start:end])
            elif isinstance(node, ast.Expr) and isinstance(
                node.value, (ast.Constant,)
            ):
                top_level_ranges.append((start, end))
                header_lines.extend(all_lines[start:end])
            else:
                top_level_ranges.append((start, end))
                body_lines.extend(all_lines[start:end])

    header_text = "".join(header_lines).strip()
    if header_text:
        header_frag = NodeFragment(
            node_id=_node_id(file_path, "module_header", "__header__"),
            kind="module_header",
            source=header_text,
            version=1,
        )
        fragments.insert(0, header_frag)

    body_text = "".join(body_lines).strip()
    if body_text:
        body_frag = NodeFragment(
            node_id=_node_id(file_path, "module_body", "__body__"),
            kind="module_body",
            source=body_text,
            version=1,
        )
        fragments.append(body_frag)

    return fragments


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
            if any(
                fnmatch.fnmatch(rel, ep)
                or (ep.startswith("**/") and fnmatch.fnmatch(rel, ep[3:]))
                for ep in exclude_patterns
            ):
                continue
            if rel in result:
                continue
            try:
                fragments = parse_file_into_fragments(rel, path.read_text("utf-8"))
                result[rel] = fragments
            except SyntaxError:
                continue

    return result
