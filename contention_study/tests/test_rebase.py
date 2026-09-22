"""Tests for the common-base merge, which is where the study's verdicts come from.

These pin the bug that made the first run of this study unusable: merging two PR
heads directly charges each side with every mainline commit that landed between
the two fork points, so pairs that share no file at all are reported as
conflicting.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from mining import window_analysis
from mining.git_repo import GitRepo
from mining.rebase import (
    VERDICT_CLEAN,
    VERDICT_CONFLICT,
    ChangeRef,
    latest_fork,
    merge_on_common_base,
    shared_base,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True
    )
    return result.stdout.decode().strip()


@pytest.fixture()
def timeline(tmp_path: Path) -> Iterator[tuple[GitRepo, Path, dict[str, str]]]:
    """Build a mainline of two commits, with one change forked off each.

    ``main1`` and ``main2`` are mainline. ``a`` forks at ``main1`` and edits
    ``alpha.py``; ``b`` forks at ``main2`` and edits ``beta.py``. The two changes
    share no file, but mainline moved ``shared.py`` in between.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main", ".")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    for name in ("alpha.py", "beta.py", "shared.py"):
        (work / name).write_text("value = 0\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "main1")
    main1 = _git(work, "rev-parse", "HEAD")

    (work / "shared.py").write_text("value = 1\n")
    _git(work, "commit", "-qam", "main2")
    main2 = _git(work, "rev-parse", "HEAD")

    _git(work, "checkout", "-q", "-b", "a", main1)
    (work / "alpha.py").write_text("value = 100\n")
    _git(work, "commit", "-qam", "change a")
    head_a = _git(work, "rev-parse", "HEAD")

    _git(work, "checkout", "-q", "-b", "b", main2)
    (work / "beta.py").write_text("value = 200\n")
    _git(work, "commit", "-qam", "change b")
    head_b = _git(work, "rev-parse", "HEAD")

    _git(work, "checkout", "-q", "main")
    commits = {"main1": main1, "main2": main2, "head_a": head_a, "head_b": head_b}
    yield GitRepo(work / ".git"), work, commits


def test_shared_base_is_the_later_fork(
    timeline: tuple[GitRepo, Path, dict[str, str]]
) -> None:
    repo, _work, c = timeline
    assert shared_base(repo, c["main1"], c["main2"], {}) == c["main2"]
    assert shared_base(repo, c["main2"], c["main1"], {}) == c["main2"]
    assert shared_base(repo, c["main1"], c["main1"], {}) == c["main1"]


def test_disjoint_changes_off_different_forks_merge_cleanly(
    timeline: tuple[GitRepo, Path, dict[str, str]]
) -> None:
    repo, _work, c = timeline
    outcome = merge_on_common_base(
        repo,
        ChangeRef(1, c["head_a"], c["main1"]),
        ChangeRef(2, c["head_b"], c["main2"]),
        {},
    )
    assert outcome.verdict == VERDICT_CLEAN


def test_the_merged_tree_keeps_both_changes_and_mainline(
    timeline: tuple[GitRepo, Path, dict[str, str]]
) -> None:
    repo, _work, c = timeline
    outcome = merge_on_common_base(
        repo,
        ChangeRef(1, c["head_a"], c["main1"]),
        ChangeRef(2, c["head_b"], c["main2"]),
        {},
    )
    assert repo.blob(outcome.merged_tree, "alpha.py") == "value = 100\n"
    assert repo.blob(outcome.merged_tree, "beta.py") == "value = 200\n"
    assert repo.blob(outcome.merged_tree, "shared.py") == "value = 1\n"


def test_a_real_same_line_conflict_is_still_detected(
    timeline: tuple[GitRepo, Path, dict[str, str]]
) -> None:
    repo, work, c = timeline
    _git(work, "checkout", "-q", "-b", "c", c["main2"])
    (work / "beta.py").write_text("value = 999\n")
    _git(work, "commit", "-qam", "change c")
    head_c = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")

    outcome = merge_on_common_base(
        repo,
        ChangeRef(2, c["head_b"], c["main2"]),
        ChangeRef(3, head_c, c["main2"]),
        {},
    )
    assert outcome.verdict == VERDICT_CONFLICT
    assert outcome.conflicted_paths == ("beta.py",)


def test_plain_merge_tree_would_have_blamed_mainline(
    timeline: tuple[GitRepo, Path, dict[str, str]]
) -> None:
    """The control: the naive call charges side A with mainline's ``shared.py``."""
    repo, work, c = timeline
    _git(work, "checkout", "-q", "-b", "d", c["main1"])
    (work / "shared.py").write_text("value = 42\n")
    _git(work, "commit", "-qam", "change d touching shared")
    head_d = _git(work, "rev-parse", "HEAD")
    _git(work, "checkout", "-q", "main")

    naive = repo.merge_tree(head_d, c["head_b"])
    assert not naive.clean, "expected the naive merge to report a mainline conflict"

    corrected = merge_on_common_base(
        repo,
        ChangeRef(4, head_d, c["main1"]),
        ChangeRef(2, c["head_b"], c["main2"]),
        {},
    )
    assert corrected.verdict != VERDICT_CONFLICT


def test_latest_fork_picks_the_most_recent_mainline_commit(
    timeline: tuple[GitRepo, Path, dict[str, str]]
) -> None:
    repo, _work, c = timeline
    assert latest_fork(repo, (c["main1"], c["main2"], c["main1"]), {}) == c["main2"]
    assert latest_fork(repo, (c["main1"],), {}) == c["main1"]


def test_window_replay_does_not_count_git_failure_as_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable git object store must fail a window, not inflate RQ5."""

    class FailingRepo:
        def merge_tree(self, _a: str, _b: str, *, base: str) -> object:
            del base
            return type(
                "FailedVerdict",
                (),
                {"failed": True, "clean": False, "tree": ""},
            )()

    monkeypatch.setattr(window_analysis, "_WORKER_REPO", FailingRepo())
    monkeypatch.setattr(window_analysis, "earliest_fork", lambda *_args: "base")
    monkeypatch.setattr(window_analysis, "tree_of", lambda *_args: "root")
    trees = iter((("tree-a", ""), ("tree-b", "")))
    monkeypatch.setattr(window_analysis, "port_to_base", lambda *_args: next(trees))

    result = window_analysis._worker_sequence(
        (
            2,
            (
                ChangeRef(1, "head-a", "fork-a"),
                ChangeRef(2, "head-b", "fork-b"),
            ),
        )
    )

    assert result == (2, 0, 0, 2, True)
