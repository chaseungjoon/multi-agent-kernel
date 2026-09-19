"""Materialize a project state that never existed on disk (Wave 20, D4/D6).

"Does this test pass with task A alone? With B alone? With both?" is only
cheap to ask because MAK holds every committed node version: any subset of a
wave's commits can be assembled without git. An overlay turns such a subset
into a directory a subprocess can import and test — a copy of the working
tree with chosen files replaced (or removed) — and deletes it afterwards.

Copies skip everything that is never project source (VCS metadata, virtual
environments, caches, MAK's own directory), so an overlay costs the size of
the project's sources, not of its environment.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

_NEVER_COPIED = (
    ".git", ".hg", ".mak", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "build", "dist",
    "*.egg-info",
)


@contextlib.contextmanager
def overlay(
    work_dir: Path,
    files: Mapping[str, str | None],
    *,
    extra_ignored: tuple[str, ...] = (),
) -> Iterator[Path]:
    """Yield a temporary copy of ``work_dir`` with ``files`` substituted.

    ``files`` maps a work-dir-relative path to its content, or to None to
    remove it (the file did not exist in that state). The copy is removed on
    exit, whatever happens inside the block.
    """
    with tempfile.TemporaryDirectory(prefix="mak-overlay-") as tmp:
        root = Path(tmp) / "project"
        shutil.copytree(
            work_dir,
            root,
            symlinks=True,
            ignore=shutil.ignore_patterns(*_NEVER_COPIED, *extra_ignored),
        )
        for rel, content in files.items():
            target = root / rel
            if content is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        yield root
