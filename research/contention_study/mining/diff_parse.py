"""Parse ``git diff -U0 -M`` output into per-file hunk records.

Zero context lines mean every ``@@`` range is exactly the changed region, which
is what makes the hunk-to-node mapping tight: with the default three context
lines a one-line edit would appear to touch its neighbours.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mining.records import Hunk

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DEV_NULL = "/dev/null"


@dataclass(frozen=True, slots=True)
class FileDiff:
    """One file's entry in a diff.

    ``old_path`` is None for an added file and ``new_path`` is None for a deleted
    one. A rename has both set to different values.
    """

    old_path: str | None
    new_path: str | None
    hunks: tuple[Hunk, ...]
    binary: bool

    @property
    def path(self) -> str:
        """The path the change should be attributed to (the new one if it exists)."""
        return self.new_path or self.old_path or ""

    @property
    def renamed(self) -> bool:
        """Whether git detected this file as a rename."""
        return (
            self.old_path is not None
            and self.new_path is not None
            and self.old_path != self.new_path
        )


def _strip_prefix(raw: str) -> str | None:
    """Turn a ``--- a/path`` / ``+++ b/path`` operand into a repo-relative path."""
    operand = raw.split("\t", 1)[0].strip()
    if operand == _DEV_NULL:
        return None
    if len(operand) > 2 and operand[1] == "/":
        return operand[2:]
    return operand


def _split_git_header(raw: str) -> tuple[str | None, str | None]:
    """Recover both paths from a ``diff --git a/X b/Y`` line.

    Paths may contain spaces, so every ``" b/"`` split point is tried and the one
    whose halves are consistent wins. Returns ``(None, None)`` when no split works.
    """
    body = raw[len("diff --git ") :]
    if not body.startswith("a/"):
        return None, None
    for match in re.finditer(r" b/", body):
        left = body[2 : match.start()]
        right = body[match.end() :]
        if left == right or left.count("/") >= 0:
            return left, right
    return None, None


def parse_diff(text: str) -> list[FileDiff]:
    """Parse a whole ``git diff`` into :class:`FileDiff` records, in file order."""
    files: list[FileDiff] = []
    old_path: str | None = None
    new_path: str | None = None
    header_old: str | None = None
    header_new: str | None = None
    hunks: list[Hunk] = []
    binary = False
    started = False

    def flush() -> None:
        nonlocal old_path, new_path, header_old, header_new, hunks, binary, started
        if started:
            resolved_old = old_path if old_path is not None else header_old
            resolved_new = new_path if new_path is not None else header_new
            if seen_old_marker and old_path is None:
                resolved_old = None
            if seen_new_marker and new_path is None:
                resolved_new = None
            files.append(
                FileDiff(
                    old_path=resolved_old,
                    new_path=resolved_new,
                    hunks=tuple(hunks),
                    binary=binary,
                )
            )
        old_path = new_path = header_old = header_new = None
        hunks = []
        binary = False
        started = False

    seen_old_marker = False
    seen_new_marker = False

    for line in text.splitlines():
        if line.startswith("diff --git "):
            flush()
            seen_old_marker = seen_new_marker = False
            header_old, header_new = _split_git_header(line)
            started = True
        elif not started:
            continue
        elif line.startswith("rename from "):
            header_old = line[len("rename from ") :]
        elif line.startswith("rename to "):
            header_new = line[len("rename to ") :]
        elif line.startswith("--- "):
            seen_old_marker = True
            old_path = _strip_prefix(line[4:])
        elif line.startswith("+++ "):
            seen_new_marker = True
            new_path = _strip_prefix(line[4:])
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            binary = True
        else:
            match = _HUNK.match(line)
            if match is None:
                continue
            old_start = int(match.group(1))
            old_count = 1 if match.group(2) is None else int(match.group(2))
            new_start = int(match.group(3))
            new_count = 1 if match.group(4) is None else int(match.group(4))
            path = new_path or header_new or ""
            hunks.append(
                Hunk(
                    path=path,
                    old_path=old_path or header_old or path,
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                )
            )
    flush()
    return files
