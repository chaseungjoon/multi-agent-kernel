"""Tests for mak.git_integration.git using temporary git repositories."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from mak.core.exceptions import GitIntegrationError
from mak.git_integration.git import GitHelper


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    """Create an initialized git repo with an identity and one initial commit."""
    work = tmp_path / "repo"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "test@mak.dev")
    _git(work, "config", "user.name", "MAK Test")
    (work / "README.md").write_text("init\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial commit")
    yield work


def _write(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    return name


class TestCommitTask:
    def test_commit_returns_hash(self, repo: Path) -> None:
        helper = GitHelper(repo)
        f = _write(repo, "a.py", "x = 1\n")
        commit_hash = helper.commit_task(
            "042", [f], "implement a", "anthropic_api", "mak-session-1"
        )
        assert commit_hash is not None
        assert len(commit_hash) == 40
        assert helper.validate_clean_state()

    def test_empty_diff_commit_returns_none(self, repo: Path) -> None:
        # Committing a file whose content is byte-identical to HEAD is a no-op,
        # not an error: commit_task returns None instead of crashing (RA-8).
        helper = GitHelper(repo)
        f = _write(repo, "a.py", "x = 1\n")
        first = helper.commit_task("1", [f], "first", "x", "s1")
        assert first is not None
        again = helper.commit_task("2", [f], "again", "x", "s1")
        assert again is None

    def test_commit_message_format(self, repo: Path) -> None:
        helper = GitHelper(repo)
        f = _write(repo, "a.py", "x = 1\n")
        helper.commit_task("042", [f], "implement a", "claude_code", "mak-session-1")
        msg = subprocess.run(
            ["git", "log", "-1", "--format=%s%n%b"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert msg.startswith("[MAK-042] implement a")
        assert "Agent: claude_code" in msg
        assert "Session: mak-session-1" in msg
        assert "Files: a.py" in msg

    def test_custom_prefix(self, repo: Path) -> None:
        helper = GitHelper(repo, commit_prefix="[KERNEL]")
        f = _write(repo, "a.py", "x = 1\n")
        helper.commit_task("7", [f], "do it", "openai_api", "s1")
        subject = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert subject == "[KERNEL-7] do it"

    def test_commit_with_no_files_raises(self, repo: Path) -> None:
        with pytest.raises(GitIntegrationError, match="no files"):
            GitHelper(repo).commit_task("1", [], "d", "a", "s")

    def test_multiple_files_committed(self, repo: Path) -> None:
        helper = GitHelper(repo)
        f1 = _write(repo, "a.py", "x = 1\n")
        f2 = _write(repo, "b.py", "y = 2\n")
        helper.commit_task("1", [f1, f2], "two files", "anthropic_api", "s1")
        assert helper.validate_clean_state()


class TestGetSessionCommits:
    def test_filters_by_session(self, repo: Path) -> None:
        helper = GitHelper(repo)
        helper.commit_task("1", [_write(repo, "a.py", "1\n")], "a", "x", "session-A")
        helper.commit_task("2", [_write(repo, "b.py", "2\n")], "b", "y", "session-B")
        helper.commit_task("3", [_write(repo, "c.py", "3\n")], "c", "z", "session-A")

        a_commits = helper.get_session_commits("session-A")
        assert {c.task_id for c in a_commits} == {"1", "3"}
        assert all(c.session_id == "session-A" for c in a_commits)

    def test_parses_agent_and_task_id(self, repo: Path) -> None:
        helper = GitHelper(repo)
        helper.commit_task(
            "099", [_write(repo, "a.py", "1\n")], "do a", "openai_api", "s1"
        )
        (commit,) = helper.get_session_commits("s1")
        assert commit.task_id == "099"
        assert commit.agent_type == "openai_api"
        assert commit.subject == "[MAK-099] do a"

    def test_unknown_session_returns_empty(self, repo: Path) -> None:
        helper = GitHelper(repo)
        helper.commit_task("1", [_write(repo, "a.py", "1\n")], "a", "x", "s1")
        assert helper.get_session_commits("nope") == []


class TestValidateCleanState:
    def test_clean_after_commit(self, repo: Path) -> None:
        assert GitHelper(repo).validate_clean_state()

    def test_dirty_with_untracked(self, repo: Path) -> None:
        _write(repo, "dirty.py", "1\n")
        assert not GitHelper(repo).validate_clean_state()

    def test_dirty_with_modification(self, repo: Path) -> None:
        (repo / "README.md").write_text("changed\n")
        assert not GitHelper(repo).validate_clean_state()


class TestPush:
    def test_push_to_bare_remote(self, repo: Path, tmp_path: Path) -> None:
        bare = tmp_path / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(repo, "remote", "add", "origin", str(bare))
        helper = GitHelper(repo)
        helper.commit_task("1", [_write(repo, "a.py", "1\n")], "a", "x", "s1")
        helper.push("main")
        # The remote now has the pushed branch.
        refs = subprocess.run(
            ["git", "branch", "--list"],
            cwd=bare,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "main" in refs

    def test_push_failure_raises(self, repo: Path) -> None:
        # No remote configured → push fails and surfaces as GitIntegrationError.
        with pytest.raises(GitIntegrationError):
            GitHelper(repo).push("main")


class TestErrors:
    def test_git_command_failure_raises(self, tmp_path: Path) -> None:
        # A directory that is not a git repo → any command fails.
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        with pytest.raises(GitIntegrationError):
            GitHelper(not_a_repo).validate_clean_state()
