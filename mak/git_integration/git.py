"""Git integration — an audit log of validated changes, not an isolation layer.

Git is *not* used for isolation or conflict resolution in MAK; lock discipline
already prevents conflicting concurrent writes. Git is a linear,
task-ordered audit trail: after MAK validates an agent's output, the change is
committed with a structured ``[MAK-<task_id>]`` message recording the agent and
session. A push is coordinated once at session end after the full suite is green.

All operations shell out to ``git`` via ``subprocess`` and surface failures as
``GitIntegrationError`` with the command's stderr — nothing is swallowed silently.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from mak.core.exceptions import GitIntegrationError

_logger = logging.getLogger(__name__)

# ASCII separators keep field/record boundaries out of commit text.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_TASK_ID_RE = re.compile(r"\[MAK-([^\]]+)\]")


@dataclass(frozen=True, slots=True)
class CommitInfo:
    """A parsed MAK commit from ``git log``."""

    hash: str
    subject: str
    task_id: str | None
    agent_type: str | None
    session_id: str | None
    body: str


class GitHelper:
    """Runs MAK's git audit-log operations against a repository directory."""

    def __init__(self, repo_dir: Path, commit_prefix: str = "[MAK]") -> None:
        self._repo_dir = repo_dir
        self._commit_prefix = commit_prefix

    def _run(self, args: list[str], *, env: dict[str, str] | None = None) -> str:
        """Run a git subcommand, returning stdout or raising on failure.

        ``env`` overlays the process environment — in practice always
        ``GIT_INDEX_FILE``, which is what keeps an audit commit out of the user's
        own index.
        """
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self._repo_dir,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, **env} if env else None,
            )
        except FileNotFoundError as exc:
            raise GitIntegrationError("git executable not found on PATH") from exc
        if result.returncode != 0:
            raise GitIntegrationError(
                f"git {' '.join(args)} failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    def ensure_initialized(self) -> bool:
        """Ensure the work-dir is its *own* git repo before any audit commit.

        MAK uses git as a per-project audit log. If the work-dir is not itself a
        repository root — e.g. it is nested inside an outer repo such as a home
        directory — committing there would leak MAK's commits into that surrounding
        repo. In that case (or when the dir is in no repo at all) ``git init`` the
        work-dir so commits land in the project. Returns ``True`` if it initialized
        a new repo, ``False`` if the dir was already its own repo root.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self._repo_dir,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitIntegrationError("git executable not found on PATH") from exc
        own_root = self._repo_dir.resolve()
        if result.returncode == 0 and Path(result.stdout.strip()).resolve() == own_root:
            return False
        self._run(["init", "-q"])
        self._ensure_identity()
        return True

    def _ensure_identity(self) -> None:
        """Set a *local* commit identity only if git cannot resolve one already.

        A repo MAK just created has no identity; without one ``git commit`` fails. We
        only set a fallback when none is configured at any scope, so a user's global
        identity is always preserved and used when present.
        """
        for key, fallback in (("user.email", "mak@local"), ("user.name", "MAK")):
            existing = subprocess.run(
                ["git", "config", key],
                cwd=self._repo_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if existing.returncode != 0 or not existing.stdout.strip():
                self._run(["config", key, fallback])

    def _subject(self, task_id: str, description: str) -> str:
        """Build the commit subject, embedding the task id in the prefix."""
        prefix = self._commit_prefix
        if prefix.endswith("]"):
            return f"{prefix[:-1]}-{task_id}] {description}"
        return f"{prefix} [{task_id}] {description}"

    def commit_task(
        self,
        task_id: str,
        files: list[str],
        description: str,
        agent_type: str,
        session_id: str,
    ) -> str | None:
        """Commit exactly ``files`` with MAK's structured message.

        Returns the new commit's full hash, or ``None`` if those files are
        byte-identical to HEAD (an empty diff) — that is a no-op, not an error, so
        a reconstruction that changed nothing does not crash the session.

        **The user's index is never touched.** This used to run ``git add`` on the
        real index, check the *whole* index for staged content, and then run an
        unrestricted ``git commit``, with two consequences: a user's staged
        ``unrelated.txt`` was swept into MAK's audit commit, and a task whose own
        files had not changed still committed whatever else the user had staged.
        Neither is something an audit log is entitled to do.

        Instead the commit is built in a **private index** (``GIT_INDEX_FILE``)
        seeded from HEAD, so the resulting tree is exactly "HEAD plus these
        files" no matter what the user has staged. The temporary index is removed
        on every path, including failure, so a Git error leaves the user's own
        index byte-identical to what it was before the call.

        **Pre-existing edits to task-owned files.** What is committed is the
        working-tree content MAK materialized, which after startup reconciliation
        already incorporates any uncommitted edit the user had made to that file.
        A partially-staged version of a task-owned file is therefore not what
        lands in the commit — the file on disk is — and the user's index entry
        for it is left alone.
        """
        if not files:
            raise GitIntegrationError(
                f"task '{task_id}' has no files to commit"
            )
        index_path = self._repo_dir / ".git" / f"mak-index-{uuid.uuid4().hex}"
        env = {"GIT_INDEX_FILE": str(index_path)}
        try:
            self._seed_index(env)
            self._run(["add", "--", *files], env=env)
            if not self._index_differs_from_head(env):
                return None
            body = (
                f"Files: {', '.join(files)}\n"
                f"Status: complete\n"
                f"Agent: {agent_type}\n"
                f"Session: {session_id}"
            )
            self._run(
                ["commit", "-m", self._subject(task_id, description), "-m", body],
                env=env,
            )
            commit_hash = self._run(["rev-parse", "HEAD"]).strip()
        finally:
            index_path.unlink(missing_ok=True)
        self._refresh_user_index(files)
        return commit_hash

    def _has_head(self) -> bool:
        """Whether the repo has a HEAD commit yet (a fresh ``git init`` has none)."""
        try:
            self._run(["rev-parse", "--verify", "HEAD"])
        except GitIntegrationError:
            return False
        return True

    def _seed_index(self, env: dict[str, str]) -> None:
        """Fill the private index with HEAD, so the commit is HEAD + our files."""
        if self._has_head():
            self._run(["read-tree", "HEAD"], env=env)
        else:
            self._run(["read-tree", "--empty"], env=env)

    def _index_differs_from_head(self, env: dict[str, str]) -> bool:
        """Whether the *private* index has content HEAD does not.

        Scoped twice over: to the index this commit is being built in, and to
        HEAD. The old check asked whether *anything at all* was staged anywhere,
        which is how a task with no changes of its own could commit somebody
        else's work.
        """
        if not self._has_head():
            return bool(self._run(["ls-files", "--cached"], env=env).strip())
        try:
            result = subprocess.run(
                ["git", "diff-index", "--cached", "--quiet", "HEAD", "--"],
                cwd=self._repo_dir,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, **env},
            )
        except FileNotFoundError as exc:
            raise GitIntegrationError("git executable not found on PATH") from exc
        # 0 → index matches HEAD; 1 → it differs; anything else → error.
        if result.returncode == 0:
            return False
        if result.returncode == 1:
            return True
        raise GitIntegrationError(
            f"git diff-index failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    def _refresh_user_index(self, files: list[str]) -> None:
        """Re-stat *only* the committed paths in the user's real index.

        Without this the user's index still holds the pre-commit blobs for those
        paths, so ``git status`` would report MAK's own commit back to them as
        staged modifications. Scoped to the committed files, so unrelated staged
        work is untouched.

        A failure here is a warning, not an error: the commit has already landed
        and the working tree is correct, so the worst case is a cosmetically
        stale index entry the user's next ``git add`` fixes.
        """
        try:
            self._run(["update-index", "--add", "--", *files])
        except GitIntegrationError as exc:
            _logger.warning(
                "committed the audit entry but could not refresh the index for "
                "%s: %s. `git status` may show them as staged until you re-add.",
                ", ".join(files),
                exc,
            )

    def get_session_commits(self, session_id: str) -> list[CommitInfo]:
        """Return all commits whose body records ``session_id``, newest first."""
        fmt = _FIELD_SEP.join(["%H", "%s", "%b"]) + _RECORD_SEP
        out = self._run(["log", f"--format={fmt}"])
        commits: list[CommitInfo] = []
        for record in out.split(_RECORD_SEP):
            record = record.strip("\n")
            if not record.strip():
                continue
            commit = self._parse_record(record)
            if commit is not None and commit.session_id == session_id:
                commits.append(commit)
        return commits

    @staticmethod
    def _parse_record(record: str) -> CommitInfo | None:
        parts = record.split(_FIELD_SEP)
        if len(parts) < 3:
            return None
        commit_hash, subject, body = parts[0], parts[1], parts[2]
        task_match = _TASK_ID_RE.search(subject)
        return CommitInfo(
            hash=commit_hash.strip(),
            subject=subject.strip(),
            task_id=task_match.group(1) if task_match else None,
            agent_type=_field_from_body(body, "Agent"),
            session_id=_field_from_body(body, "Session"),
            body=body.strip(),
        )

    def validate_clean_state(self) -> bool:
        """Return True if the working tree has no uncommitted changes."""
        return self._run(["status", "--porcelain"]).strip() == ""

    def push(self, branch: str | None = None, remote: str = "origin") -> None:
        """Push to ``remote`` (optionally a specific ``branch``)."""
        args = ["push", remote]
        if branch is not None:
            args.append(branch)
        self._run(args)


def _field_from_body(body: str, label: str) -> str | None:
    """Extract a ``Label: value`` line value from a commit body."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{label}:"):
            return stripped[len(label) + 1 :].strip()
    return None
