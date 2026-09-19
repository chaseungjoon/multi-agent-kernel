"""Walk a project's Python files and its import graph, for the gates."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from mak.conflict_detector.cycle_check import import_graph

_SKIPPED_DIRS = frozenset({
    ".git", ".hg", ".mak", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "build", "dist",
    "site-packages",
})


def python_sources(root: Path) -> dict[str, str]:
    """Every readable ``. py`` file under ``root``, keyed by relative path."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        skipped = any(
            part in _SKIPPED_DIRS or part.endswith(".egg-info")
            for part in rel.parts
        )
        if skipped:
            continue
        try:
            found[rel.as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return found


def importers_of(sources: dict[str, str], targets: Iterable[str]) -> set[str]:
    """Files that import any of ``targets``, directly or transitively."""
    graph = import_graph(sources)
    reverse: dict[str, set[str]] = {}
    for src, edges in graph.items():
        for dst, _is_name in edges:
            reverse.setdefault(dst, set()).add(src)
    seen: set[str] = set()
    stack = list(targets)
    while stack:
        node = stack.pop()
        for importer in reverse.get(node, ()):
            if importer not in seen:
                seen.add(importer)
                stack.append(importer)
    return seen


def is_test_file(path: str) -> bool:
    """Pytest's default discovery rule for test modules."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def module_name(path: str, root: Path) -> str | None:
    """Return the dotted module a file imports as, from the project root or ``src/``."""
    if not path.endswith(".py"):
        return None
    rel = path[:-3]
    if rel.startswith("src/") and (root / "src").is_dir():
        rel = rel[len("src/"):]
    parts = rel.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)
