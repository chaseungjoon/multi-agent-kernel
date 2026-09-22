"""Stage 23.1a — page PR metadata from the GitHub API into the SQLite cache.

Walks the closed-PR list newest-first and stops once the study window is covered
or the per-repo cap is reached. Resumable: PRs already stored are re-upserted
rather than refetched from scratch, and the fetch date is recorded in
``run_meta`` for reproducibility.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from mining.cache import CacheHandle, get_meta, open_cache, set_meta, table_count
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.github_api import GitHubClient
from mining.records import PullRequestRecord, parse_ts


def _to_record(raw: dict[str, Any]) -> PullRequestRecord:
    """Convert one API PR object into a :class:`PullRequestRecord`."""
    user = raw.get("user") or {}
    return PullRequestRecord(
        number=int(raw["number"]),
        created_at=str(raw["created_at"]),
        merged_at=raw.get("merged_at"),
        closed_at=raw.get("closed_at"),
        merged=raw.get("merged_at") is not None,
        base_ref=str((raw.get("base") or {}).get("ref", "")),
        base_sha=str((raw.get("base") or {}).get("sha", "")),
        head_sha=str((raw.get("head") or {}).get("sha", "")),
        user_login=str(user.get("login", "")),
        user_type=str(user.get("type", "")),
        draft=bool(raw.get("draft", False)),
    )


def _store(handle: CacheHandle, records: list[PullRequestRecord]) -> None:
    """Upsert a page of PR records."""
    handle.conn.executemany(
        "INSERT INTO pull_request (number, created_at, merged_at, closed_at, merged,"
        " base_ref, base_sha, head_sha, user_login, user_type, draft)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(number) DO UPDATE SET"
        "  merged_at=excluded.merged_at, closed_at=excluded.closed_at,"
        "  merged=excluded.merged, head_sha=excluded.head_sha",
        [
            (
                r.number, r.created_at, r.merged_at, r.closed_at, int(r.merged),
                r.base_ref, r.base_sha, r.head_sha, r.user_login, r.user_type,
                int(r.draft),
            )
            for r in records
        ],
    )
    handle.conn.commit()


def fetch_repo(
    config: StudyConfig,
    spec: RepoSpec,
    client: GitHubClient,
    *,
    force: bool = False,
    verbose: bool = True,
) -> int:
    """Fetch closed PRs for one repository; return how many are now cached.

    The walk is newest-first over ``created_at`` and stops at the configured
    ``since`` boundary or once ``max_prs_per_repo`` *merged* PRs inside the
    window have been seen, whichever comes first.
    """
    handle = open_cache(config, spec)
    total = table_count(handle, "pull_request")
    same_window = (
        get_meta(handle, "window_since") == config.since
        and get_meta(handle, "window_until") == config.until
    )
    if total and same_window and get_meta(handle, "fetch_prs_at") and not force:
        if verbose:
            print(f"  {spec.slug}: {total} PR rows cached, skipping", flush=True)
        handle.conn.close()
        return total

    since = parse_ts(f"{config.since}T00:00:00Z")
    until = parse_ts(f"{config.until}T23:59:59Z")
    merged_in_window = 0
    seen = 0

    path = f"/repos/{spec.slug}/pulls?state=closed&sort=created&direction=desc"
    for page in client.paginate(path):
        if not page:
            break
        records = [_to_record(raw) for raw in page]
        _store(handle, records)
        seen += len(records)
        oldest = parse_ts(records[-1].created_at)
        for record in records:
            if not record.merged or record.merged_at is None:
                continue
            merged_at = parse_ts(record.merged_at)
            if since <= merged_at <= until:
                merged_in_window += 1
        if verbose:
            print(
                f"  {spec.slug}: {seen} closed PRs seen, "
                f"{merged_in_window} merged in window, oldest {oldest.date()}",
                flush=True,
            )
        if merged_in_window >= config.max_prs_per_repo or oldest < since:
            break

    set_meta(handle, "fetch_prs_at", datetime.now(UTC).isoformat())
    set_meta(handle, "window_since", config.since)
    set_meta(handle, "window_until", config.until)
    set_meta(handle, "closed_prs_seen", str(seen))
    total = table_count(handle, "pull_request")
    handle.conn.close()
    return total


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.fetch_prs [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="refresh metadata even when the configured window is cached",
    )
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    client = GitHubClient()
    for spec in specs:
        print(f"fetching {spec.slug} ...", flush=True)
        total = fetch_repo(config, spec, client, force=args.force)
        print(f"  -> {total} PRs cached", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
