"""Stage 23.7a — bucket PRs and paths, reporting counts instead of dropping silently.

Nothing is deleted from the cache. Every PR gets a ``bucket`` label and every
bucket's size is recorded in ``run_meta``, so the write-up can state exactly how
many changes each filter removed and a reader can recompute the headline numbers
with a different filter set.
"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass

from mining.cache import CacheHandle, open_cache, set_meta
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.git_repo import GitRepo

BUCKET_KEPT = "kept"
BUCKET_BOT = "bot"
BUCKET_OVERSIZE = "oversize"
BUCKET_WHITESPACE = "whitespace_sweep"
BUCKET_UNUSABLE = "unusable"
BUCKET_EMPTY = "no_footprint"

# Substrings that identify an automated author. GitHub's own ``user.type == "Bot"``
# catches apps; these catch the service accounts that push as ordinary users.
_BOT_MARKERS = (
    "[bot]", "dependabot", "renovate", "pre-commit-ci", "github-actions",
    "codecov", "mergify", "allcontributors", "sourcery-ai", "pyup-bot",
    "imgbot", "restyled", "snyk-bot", "greenkeeper", "weblate", "transifex",
)

# Paths whose contents are generated or machine-maintained. Contention in these
# is real but says nothing about how developers divide work, so they are excluded
# from node sets and counted separately.
_GENERATED_GLOBS = (
    "*.lock", "*.min.js", "*.min.css", "*.mo", "*.po", "*.pot",
    "*_pb2.py", "*_pb2_grpc.py", "*.pyi.in", "*.generated.*",
    "**/migrations/[0-9][0-9][0-9][0-9]_*.py",
    "**/node_modules/**", "**/vendor/**", "**/_vendor/**",
    # Directories whose contents a code generator rewrites wholesale
    # (home-assistant's hassfest output, protobuf stubs, API snapshots).
    "**/generated/*", "**/generated/**/*", "**/_generated/*",
    "uv.lock", "poetry.lock", "package-lock.json", "yarn.lock", "Cargo.lock",
    "AUTHORS", "CONTRIBUTORS", "*.snap",
)

# A PR past either bound is a sweep or a release, not a development task.
OVERSIZE_FILES = 100
OVERSIZE_LINES = 5000
# Only PRs at least this wide are worth a second diff to test for a format sweep.
WHITESPACE_PROBE_FILES = 20
# Below this share of surviving changed lines under ``diff -w``, the PR is
# overwhelmingly whitespace.
WHITESPACE_SURVIVAL = 0.2


@dataclass(frozen=True, slots=True)
class FilterCounts:
    """How many PRs landed in each bucket, in filter order."""

    buckets: tuple[tuple[str, int], ...]

    def as_json(self) -> str:
        """Serialise for ``run_meta`` so the write-up can quote exact counts."""
        return json.dumps(dict(self.buckets))


def is_bot(login: str, user_type: str) -> bool:
    """Whether a PR author is automation rather than a person."""
    if user_type.lower() == "bot":
        return True
    lowered = login.lower()
    return any(marker in lowered for marker in _BOT_MARKERS)


def is_generated(path: str) -> bool:
    """Whether a path is generated or machine-maintained content."""
    return any(
        fnmatch.fnmatch(path, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]))
        for pattern in _GENERATED_GLOBS
    )


def _whitespace_sweep(repo: GitRepo, fork: str, head: str) -> bool:
    """Whether almost all of a change disappears under ``git diff -w``."""
    full = repo.numstat(fork, head)
    total = sum(a + r for a, r, _ in full if a >= 0 and r >= 0)
    if total == 0:
        return False
    stripped = repo.text(
        "-c", "core.quotePath=false", "diff", "--numstat", "-M", "-w", fork, head
    )
    survived = 0
    for line in stripped.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] != "-" and parts[1] != "-":
            survived += int(parts[0]) + int(parts[1])
    return survived < WHITESPACE_SURVIVAL * total


def classify_repo(
    config: StudyConfig, spec: RepoSpec, *, verbose: bool = True
) -> FilterCounts:
    """Assign a bucket to every mapped PR of one repository."""
    handle = open_cache(config, spec)
    repo = GitRepo(config.clone_dir(spec))
    rows = handle.conn.execute(
        "SELECT ch.number, ch.status, ch.files_total, ch.lines_added,"
        "       ch.lines_removed, ch.nodes_total, ch.fork_point, ch.head_sha,"
        "       pr.user_login, pr.user_type"
        " FROM pr_change ch JOIN pull_request pr ON pr.number = ch.number"
    ).fetchall()

    counts: Counter[str] = Counter()
    updates: list[tuple[str, int]] = []
    for row in rows:
        bucket = _bucket_for(repo, row)
        counts[bucket] += 1
        updates.append((bucket, int(row["number"])))
    handle.conn.executemany("UPDATE pr_change SET bucket = ? WHERE number = ?", updates)
    handle.conn.commit()

    ordered = tuple(
        (name, counts.get(name, 0))
        for name in (
            BUCKET_KEPT, BUCKET_BOT, BUCKET_OVERSIZE,
            BUCKET_WHITESPACE, BUCKET_UNUSABLE, BUCKET_EMPTY,
        )
    )
    result = FilterCounts(buckets=ordered)
    set_meta(handle, "filter_counts", result.as_json())
    set_meta(handle, "generated_touches", str(_count_generated(handle)))
    if verbose:
        print(f"  {spec.slug}: {result.as_json()}", flush=True)
    handle.conn.close()
    return result


def _bucket_for(repo: GitRepo, row: sqlite3.Row) -> str:
    """Bucket one PR row. Order matters: the first matching filter wins."""
    data = dict(row)
    if data["status"] in ("no_head", "no_base", "unknown"):
        return BUCKET_UNUSABLE
    if is_bot(str(data["user_login"]), str(data["user_type"])):
        return BUCKET_BOT
    files = int(data["files_total"])
    churn = int(data["lines_added"]) + int(data["lines_removed"])
    oversize = files > OVERSIZE_FILES or churn > OVERSIZE_LINES
    if data["status"] == "oversize_file_level" or oversize:
        return BUCKET_OVERSIZE
    if files >= WHITESPACE_PROBE_FILES and _whitespace_sweep(
        repo, str(data["fork_point"]), str(data["head_sha"])
    ):
        return BUCKET_WHITESPACE
    if int(data["nodes_total"]) == 0:
        return BUCKET_EMPTY
    return BUCKET_KEPT


def _count_generated(handle: CacheHandle) -> int:
    """How many node touches point at generated content."""
    paths = handle.conn.execute("SELECT DISTINCT path FROM pr_node").fetchall()
    generated = {str(row["path"]) for row in paths if is_generated(str(row["path"]))}
    if not generated:
        return 0
    marks = ",".join("?" for _ in generated)
    row = handle.conn.execute(
        f"SELECT COUNT(*) AS n FROM pr_node WHERE path IN ({marks})", tuple(generated)
    ).fetchone()
    return int(row["n"])


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.filters [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        print(f"filtering {spec.slug} ...", flush=True)
        classify_repo(config, spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
