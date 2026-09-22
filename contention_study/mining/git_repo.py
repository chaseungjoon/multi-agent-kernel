"""A thin, typed wrapper over the git plumbing the study needs.

Everything runs against a **bare** clone, so no worktree is ever checked out:
``git show`` reads blobs, ``git diff -U0 -M`` produces hunks, and
``git merge-tree --write-tree`` performs a real three-way merge in memory. That
last one is why git >= 2.38 is a hard requirement.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from mining.exceptions import GitCommandError, RepositoryNotPrepared


@dataclass(frozen=True, slots=True)
class MergeVerdict:
    """The outcome of a three-way merge performed by ``git merge-tree``."""

    clean: bool
    conflicted_paths: tuple[str, ...]
    tree: str = ""
    failed: bool = False
    reason: str = ""


class GitRepo:
    """Read-only access to one bare clone."""

    def __init__(self, git_dir: Path) -> None:
        if not (git_dir / "HEAD").exists():
            raise RepositoryNotPrepared(f"{git_dir} is not a bare git repository")
        self._git_dir = git_dir

    @property
    def git_dir(self) -> Path:
        """Path of the bare clone this instance reads."""
        return self._git_dir

    def run(
        self, *args: str, check: bool = True, binary: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one git command against the bare clone.

        ``check=False`` is used where a nonzero exit is *information* (merge
        conflict, unknown object) rather than a failure.
        """
        completed = subprocess.run(
            ["git", "--git-dir", str(self._git_dir), *args],
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", "replace").strip()
            raise GitCommandError(f"git {' '.join(args)} failed: {stderr}")
        return completed

    def text(self, *args: str) -> str:
        """Run a git command and decode stdout as UTF-8 (lossy)."""
        return self.run(*args).stdout.decode("utf-8", "replace")

    def has_object(self, sha: str) -> bool:
        """Whether an object is present in this clone."""
        if not sha:
            return False
        probe = self.run("cat-file", "-e", f"{sha}^{{commit}}", check=False)
        return probe.returncode == 0

    def merge_base(self, a: str, b: str) -> str | None:
        """Best common ancestor of two commits, or None when there is none."""
        completed = self.run("merge-base", a, b, check=False)
        if completed.returncode != 0:
            return None
        return completed.stdout.decode().strip() or None

    def blob(self, commit: str, path: str) -> str | None:
        """Read one file's text at one commit, or None when it does not exist."""
        completed = self.run("show", f"{commit}:{path}", check=False, binary=True)
        if completed.returncode != 0:
            return None
        try:
            return completed.stdout.decode("utf-8")
        except UnicodeDecodeError:
            # Binary or non-UTF-8 content: not something MAK's parser can take.
            return None

    def diff_u0(self, base: str, head: str) -> str:
        """``git diff -U0 -M`` between two commits, as text.

        Zero context lines are essential: with context, a hunk's line range would
        spill into neighbouring nodes and inflate node-level contention.
        """
        return self.text(
            "-c", "core.quotePath=false",
            "diff", "-U0", "-M", "--no-color", "--no-ext-diff",
            "--diff-algorithm=histogram", base, head,
        )

    def numstat(self, base: str, head: str) -> list[tuple[int, int, str]]:
        """Per-file ``(added, removed, path)`` counts; binary files report -1."""
        rows: list[tuple[int, int, str]] = []
        numstat = self.text(
            "-c", "core.quotePath=false", "diff", "--numstat", "-M", base, head
        )
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added = -1 if parts[0] == "-" else int(parts[0])
            removed = -1 if parts[1] == "-" else int(parts[1])
            rows.append((added, removed, parts[2]))
        return rows

    def merge_tree(self, a: str, b: str, base: str | None = None) -> MergeVerdict:
        """Three-way merge ``a`` and ``b`` in memory and report conflicts.

        Exit code 0 means a clean merge, 1 means conflicts, and anything else
        means git could not perform the merge at all (missing object, unrelated
        histories) — reported as ``failed`` rather than silently counted clean.
        """
        args = ["merge-tree", "--write-tree", "--name-only"]
        if base is not None:
            args.append(f"--merge-base={base}")
        args.extend([a, b])
        completed = self.run(*args, check=False)
        stdout = completed.stdout.decode("utf-8", "replace")
        if completed.returncode == 0:
            written = stdout.strip().splitlines()
            return MergeVerdict(
                clean=True, conflicted_paths=(), tree=written[0] if written else ""
            )
        if completed.returncode == 1:
            # Output is "<tree oid>\n<conflicted path>\n...\n\n<messages>".
            body = stdout.split("\n\n", 1)[0].splitlines()
            return MergeVerdict(clean=False, conflicted_paths=tuple(body[1:]))
        reason = completed.stderr.decode("utf-8", "replace").strip()[:200]
        return MergeVerdict(
            clean=False, conflicted_paths=(), failed=True, reason=reason
        )

    def fetch_pull_refs(self, numbers: list[int], batch: int = 300) -> int:
        """Fetch ``refs/pull/<n>/head`` for the given PRs; return how many landed.

        Fetching in batches keeps the refspec list under the command-line limit
        and lets a single bad ref fail only its own batch.
        """
        landed = 0
        for start in range(0, len(numbers), batch):
            chunk = numbers[start : start + batch]
            refspecs = [f"+refs/pull/{n}/head:refs/pull/{n}/head" for n in chunk]
            completed = self.run("fetch", "--quiet", "origin", *refspecs, check=False)
            if completed.returncode != 0:
                # A whole batch can fail on one deleted PR ref; retry singly so
                # the survivors still land.
                for number in chunk:
                    single = self.run(
                        "fetch", "--quiet", "origin",
                        f"+refs/pull/{number}/head:refs/pull/{number}/head",
                        check=False,
                    )
                    landed += int(single.returncode == 0)
            else:
                landed += len(chunk)
        return landed
