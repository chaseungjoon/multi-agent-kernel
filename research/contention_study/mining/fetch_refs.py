"""Stage 23.1b — make each PR's head commit available locally, with no API calls.

GitHub exposes every PR's final head commit as ``refs/pull/<n>/head``, including
for squash-merged PRs whose commits never land on the integration branch. Fetching
those refs into a bare clone means the whole diff/merge analysis runs on local
objects: no per-PR API requests, and the study is reproducible from the recorded
clone SHA.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

from mining.cache import CacheHandle, open_cache, set_meta
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.exceptions import GitCommandError
from mining.git_repo import GitRepo
from mining.selection import selected_numbers


def ensure_clone(
    config: StudyConfig, spec: RepoSpec, *, verbose: bool = True
) -> GitRepo:
    """Return a :class:`GitRepo` for ``spec``, cloning bare on first use."""
    git_dir = config.clone_dir(spec)
    if not (git_dir / "HEAD").exists():
        git_dir.parent.mkdir(parents=True, exist_ok=True)
        if verbose:
            print(f"  cloning {spec.slug} (bare) ...", flush=True)
        completed = subprocess.run(
            ["git", "clone", "--bare", "--quiet",
             f"https://github.com/{spec.slug}.git", str(git_dir)],
            capture_output=True, check=False,
        )
        if completed.returncode != 0:
            raise GitCommandError(
                f"bare clone of {spec.slug} failed: "
                f"{completed.stderr.decode('utf-8', 'replace').strip()}"
            )
    return GitRepo(git_dir)


def main_tip(repo: GitRepo, spec: RepoSpec) -> str:
    """Resolve the integration branch tip, falling back to the clone's HEAD."""
    for candidate in (f"refs/heads/{spec.main_branch}", "HEAD"):
        completed = repo.run("rev-parse", candidate, check=False)
        if completed.returncode == 0:
            return completed.stdout.decode().strip()
    raise GitCommandError(f"cannot resolve an integration branch for {spec.slug}")


def fetch_heads(
    config: StudyConfig, spec: RepoSpec, *, verbose: bool = True
) -> tuple[int, int]:
    """Fetch pull refs for every selected PR; return ``(requested, present)``."""
    handle: CacheHandle = open_cache(config, spec)
    repo = ensure_clone(config, spec, verbose=verbose)
    numbers = selected_numbers(handle, config, include_unmerged=True)
    missing = [n for n in numbers if not _head_present(handle, repo, n)]
    if verbose:
        print(
            f"  {spec.slug}: {len(numbers)} selected PRs, "
            f"{len(missing)} heads to fetch",
            flush=True,
        )
    if missing:
        repo.fetch_pull_refs(missing)
    present = sum(1 for n in numbers if _head_present(handle, repo, n))

    set_meta(handle, "clone_main_branch", spec.main_branch)
    set_meta(handle, "clone_main_sha", main_tip(repo, spec))
    set_meta(handle, "fetch_refs_at", datetime.now(UTC).isoformat())
    set_meta(handle, "heads_requested", str(len(numbers)))
    set_meta(handle, "heads_present", str(present))
    handle.conn.close()
    return len(numbers), present


def _head_present(handle: CacheHandle, repo: GitRepo, number: int) -> bool:
    """Whether this PR's head commit is already an object in the clone."""
    row = handle.conn.execute(
        "SELECT head_sha FROM pull_request WHERE number = ?", (number,)
    ).fetchone()
    if row is None:
        return False
    return repo.has_object(str(row["head_sha"]))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.fetch_refs [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        print(f"preparing refs for {spec.slug} ...", flush=True)
        requested, present = fetch_heads(config, spec)
        print(f"  -> {present}/{requested} heads available locally", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
