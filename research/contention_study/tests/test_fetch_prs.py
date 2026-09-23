"""Tests for resumable pull-request metadata collection."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from mining.cache import open_cache, set_meta
from mining.config import RepoSpec, StudyConfig
from mining.fetch_prs import fetch_repo
from mining.github_api import GitHubClient


class _FailingClient:
    """Client double that proves a cached fetch never reaches the network."""

    def paginate(self, _path: str) -> object:
        """Fail if the cache guard attempts an API request."""
        raise AssertionError("cached metadata should not call GitHub")


def test_matching_cached_window_skips_github(tmp_path: Path) -> None:
    """A complete configured window should be returned directly from SQLite."""
    spec = RepoSpec("owner", "repo", "main")
    config = StudyConfig(
        repos=(spec,),
        since="2025-01-01",
        until="2025-12-31",
        cache_root=tmp_path / "clones",
        data_root=tmp_path / "data",
        plots_root=tmp_path / "plots",
    )
    handle = open_cache(config, spec)
    handle.conn.execute(
        "INSERT INTO pull_request VALUES (1, ?, ?, ?, 1, ?, ?, ?, ?, ?, 0)",
        (
            "2025-01-01T00:00:00Z",
            "2025-01-02T00:00:00Z",
            "2025-01-02T00:00:00Z",
            "main",
            "base",
            "head",
            "human",
            "User",
        ),
    )
    handle.conn.commit()
    set_meta(handle, "window_since", config.since)
    set_meta(handle, "window_until", config.until)
    set_meta(handle, "fetch_prs_at", "2026-09-22T00:00:00Z")
    handle.conn.close()

    client = cast(GitHubClient, _FailingClient())
    assert fetch_repo(config, spec, client, verbose=False) == 1
