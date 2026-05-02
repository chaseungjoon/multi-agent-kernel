"""Reconstruct source files from committed node store fragments."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from mak.core.types import NodeFragment


_KIND_ORDER = {
    "module_header": 0,
    "class": 1,
    "function": 2,
    "module_body": 3,
}


def _sort_key(fragment: NodeFragment) -> tuple[int, str]:
    return (_KIND_ORDER.get(fragment.kind, 99), str(fragment.node_id))


def assemble_fragments(fragments: list[NodeFragment]) -> str:
    """Assemble fragments into a single source string in AST order."""
    sorted_frags = sorted(fragments, key=_sort_key)
    parts: list[str] = []
    for frag in sorted_frags:
        parts.append(frag.source.rstrip())
    return "\n\n\n".join(parts) + "\n"


def _find_ruff() -> str:
    """Locate the ruff binary, preferring the current venv."""
    venv_ruff = Path(sys.executable).parent / "ruff"
    if venv_ruff.exists():
        return str(venv_ruff)
    return "ruff"


def format_with_ruff(source: str) -> str:
    """Format source code using ruff format."""
    try:
        result = subprocess.run(
            [_find_ruff(), "format", "-"],
            input=source,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return source


def normalize_with_ast(source: str) -> str:
    """Parse and unparse source to normalize AST structure."""
    tree = ast.parse(source)
    return ast.unparse(tree)


def reconstruct_file(
    fragments: list[NodeFragment],
    output_path: Path | None = None,
    use_ruff: bool = True,
) -> str:
    """Assemble fragments, normalize, format, and optionally write to disk."""
    assembled = assemble_fragments(fragments)

    ast.parse(assembled)

    if use_ruff:
        assembled = format_with_ruff(assembled)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(assembled, encoding="utf-8")

    return assembled
