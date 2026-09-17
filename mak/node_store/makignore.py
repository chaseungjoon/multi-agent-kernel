r"""``.makignore``: a gitignore-style list of paths MAK never ingests.

A project-root file the user owns and edits, read on every session start. It is
created on first run with MAK's own ``.mak`` directory and ``.git`` already
listed, so the store can never be fed back into itself as "source" — the
defect that nested ``.mak/node_store/.mak/node_store/…`` without bound.

The syntax is gitignore's, minus the parts that only make sense for a tree of
ignore files (there is exactly one ``.makignore``, at the work-dir root):

- blank lines and lines starting with ``#`` are skipped (``\#`` for a literal);
- ``!pattern`` re-includes what an earlier pattern ignored (``\!`` for a
  literal); the *last* matching pattern wins;
- a trailing ``/`` matches directories only;
- a pattern with a ``/`` at its start or middle is anchored to the project root,
  otherwise it matches at any depth;
- ``*`` and ``?`` never cross ``/``; ``**`` spans whole path segments;
- as in git, a file cannot be re-included when a parent directory is ignored.

This sits alongside ``node_store.exclude_patterns`` rather than replacing it:
the config list is MAK's defaults, the ``.makignore`` is the project's own. The
session's unconditional skip of its mak dir stays too — deleting ``.mak`` from
this file must not be able to reintroduce self-ingestion.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from mak.node_store.ingestion import _segment_regex

MAKIGNORE_FILENAME = ".makignore"

DEFAULT_MAKIGNORE = """\
# .makignore — paths MAK never ingests into its node store.
# Same syntax as .gitignore: one pattern per line, '#' comments, '!' negation,
# a trailing '/' for directories only, and a leading '/' to anchor to this root.

# MAK's own state. Its node store holds .py fragments; ingesting it would feed
# MAK its previous output back as source on every run.
.mak/
.git/
"""


@dataclass(frozen=True, slots=True)
class _Rule:
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Compile one normalized gitignore pattern into a relative-path matcher."""
    anchored = "/" in pattern
    parts = pattern.lstrip("/").split("/")
    out: list[str] = [] if anchored else ["(?:.*/)?"]
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            if last:
                # ``foo/**`` is everything *inside* foo; the separator was
                # already emitted by the previous segment.
                out.append(".*")
            else:
                out.append("(?:.*/)?")
            continue
        out.append(_segment_regex(part))
        if not last:
            out.append("/")
    return re.compile("".join(out) + r"\Z")


def _parse_line(line: str) -> _Rule | None:
    """Turn one ``.makignore`` line into a rule, or ``None`` for a no-op line."""
    line = re.sub(r"(?<!\\) +\Z", "", line.rstrip("\r\n"))
    if not line or line.startswith("#"):
        return None
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    elif line.startswith(("\\#", "\\!")):
        line = line[1:]
    line = line.replace("\\ ", " ")
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    if not line:
        return None
    return _Rule(_pattern_regex(line), negated=negated, dir_only=dir_only)


class MakIgnore:
    """A parsed ``.makignore``: answers whether a work-dir-relative path is ignored."""

    def __init__(self, lines: Iterable[str] = ()) -> None:
        self._rules = tuple(
            rule for rule in map(_parse_line, lines) if rule is not None
        )

    @classmethod
    def from_file(cls, path: Path) -> MakIgnore:
        """Load a ``.makignore``; a missing or unreadable one ignores nothing."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return cls()
        return cls(text.splitlines())

    def __bool__(self) -> bool:
        """Whether the file holds any rule at all."""
        return bool(self._rules)

    def matches(self, rel: str, is_dir: bool) -> bool:
        """Whether ``rel`` itself is ignored, *not* considering its parents.

        For a walk that already refuses to descend into an ignored directory, so
        every parent has been checked by the time a child is seen.
        """
        ignored = False
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(rel):
                ignored = not rule.negated
        return ignored

    def is_ignored(self, rel: str, is_dir: bool = False) -> bool:
        """Whether ``rel`` or any directory above it is ignored."""
        parts = rel.replace("\\", "/").strip("/").split("/")
        for depth in range(1, len(parts)):
            if self.matches("/".join(parts[:depth]), True):
                return True
        return self.matches("/".join(parts), is_dir)


def ensure_makignore(root: Path) -> Path:
    """Create ``root/.makignore`` with the defaults if it does not exist yet.

    Never overwrites: the file is the user's once it exists, including when they
    have emptied it.
    """
    path = root / MAKIGNORE_FILENAME
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(DEFAULT_MAKIGNORE)
    except FileExistsError:
        pass
    return path


def load_makignore(root: Path) -> MakIgnore:
    """Read ``root/.makignore``, falling back to the defaults when it is absent."""
    path = root / MAKIGNORE_FILENAME
    if not path.exists():
        return MakIgnore(DEFAULT_MAKIGNORE.splitlines())
    return MakIgnore.from_file(path)
