"""Reconstruct source files from committed node store fragments.

Fragments are reassembled in the order they are supplied — which the node store
preserves as original source order — so statement ordering, decorators, and
comments survive. Reconstruction never round-trips through ``ast.unparse()``
(which strips comments); it concatenates raw fragment text and runs
``ruff format`` to normalize whitespace.
"""

from __future__ import annotations

import ast
import logging
import subprocess
import sys
from pathlib import Path

from mak.core.types import NodeFragment

_logger = logging.getLogger(__name__)

_RUFF_TIMEOUT_S = 30


def assemble_fragments(fragments: list[NodeFragment]) -> str:
    """Concatenate fragments in the given order into a single source string.

    Order is preserved exactly as supplied — callers pass fragments in source
    order. Fragments are separated by a blank line; ``ruff format`` later
    normalizes spacing.
    """
    parts = [frag.source.rstrip() for frag in fragments if frag.source.strip()]
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def _find_ruff() -> str:
    """Locate the ruff binary, preferring the current venv."""
    venv_ruff = Path(sys.executable).parent / "ruff"
    if venv_ruff.exists():
        return str(venv_ruff)
    return "ruff"


def format_with_ruff(source: str) -> str:
    """Format source code using ``ruff format``, falling back to the input.

    A formatting failure is logged (not silently swallowed) and the unformatted
    source is returned so reconstruction still produces valid output.
    """
    try:
        result = subprocess.run(
            [_find_ruff(), "format", "-"],
            input=source,
            capture_output=True,
            text=True,
            timeout=_RUFF_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _logger.warning("ruff format unavailable, using raw source: %s", exc)
        return source
    if result.returncode != 0:
        _logger.warning(
            "ruff format failed (exit %d): %s", result.returncode, result.stderr.strip()
        )
        return source
    return result.stdout


def reconstruct_file(
    fragments: list[NodeFragment],
    output_path: Path | None = None,
    use_ruff: bool = True,
) -> str:
    """Assemble fragments in order, validate, format, and optionally write to disk."""
    assembled = assemble_fragments(fragments)

    ast.parse(assembled)  # guard: reconstructed output must be valid Python

    if use_ruff:
        assembled = format_with_ruff(assembled)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(assembled, encoding="utf-8")

    return assembled
