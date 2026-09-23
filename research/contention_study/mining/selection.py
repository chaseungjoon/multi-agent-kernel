"""Which PRs enter the study, and in what order.

Selection is deliberately a *contiguous* slice of the merge stream rather than a
random sample: concurrency structure is the object of study, and random sampling
would destroy it. The most recent ``max_prs_per_repo`` merged PRs inside the
window are taken, plus the closed-unmerged PRs overlapping that slice, which the
survivorship sensitivity analysis needs.
"""

from __future__ import annotations

import sqlite3

from mining.cache import CacheHandle
from mining.config import StudyConfig
from mining.records import PullRequestRecord


def _row_to_record(row: sqlite3.Row) -> PullRequestRecord:
    """Adapt a ``pull_request`` row to a :class:`PullRequestRecord`."""
    mapping = dict(row)
    return PullRequestRecord(
        number=int(mapping["number"]),
        created_at=str(mapping["created_at"]),
        merged_at=mapping["merged_at"],
        closed_at=mapping["closed_at"],
        merged=bool(mapping["merged"]),
        base_ref=str(mapping["base_ref"]),
        base_sha=str(mapping["base_sha"]),
        head_sha=str(mapping["head_sha"]),
        user_login=str(mapping["user_login"]),
        user_type=str(mapping["user_type"]),
        draft=bool(mapping["draft"]),
    )


def merged_slice(handle: CacheHandle, config: StudyConfig) -> list[PullRequestRecord]:
    """Return the merged PRs, oldest first, capped at ``max_prs_per_repo``."""
    rows = handle.conn.execute(
        "SELECT * FROM pull_request"
        " WHERE merged = 1 AND merged_at IS NOT NULL"
        "   AND merged_at >= ? AND merged_at <= ?"
        "   AND base_ref = ?"
        " ORDER BY merged_at DESC LIMIT ?",
        (
            f"{config.since}T00:00:00Z",
            f"{config.until}T23:59:59Z",
            handle.spec.main_branch,
            config.max_prs_per_repo,
        ),
    ).fetchall()
    records = [_row_to_record(row) for row in rows]
    records.sort(key=lambda r: (r.merged_at or "", r.number))
    return records


def unmerged_slice(
    handle: CacheHandle, config: StudyConfig, first_at: str, last_at: str
) -> list[PullRequestRecord]:
    """Closed-but-unmerged PRs whose lifetime overlaps the merged slice.

    These are the survivorship control: PRs abandoned *because of* conflicts are
    invisible in the merged stream by construction.
    """
    rows = handle.conn.execute(
        "SELECT * FROM pull_request"
        " WHERE merged = 0 AND closed_at IS NOT NULL"
        "   AND closed_at >= ? AND created_at <= ?"
        "   AND base_ref = ?",
        (first_at, last_at, handle.spec.main_branch),
    ).fetchall()
    return [_row_to_record(row) for row in rows]


def selected_numbers(
    handle: CacheHandle, config: StudyConfig, *, include_unmerged: bool
) -> list[int]:
    """PR numbers whose head commits the pipeline needs locally."""
    merged = merged_slice(handle, config)
    numbers = [record.number for record in merged]
    if include_unmerged and merged:
        window = unmerged_slice(
            handle, config, merged[0].created_at, merged[-1].merged_at or config.until
        )
        numbers.extend(record.number for record in window)
    return sorted(set(numbers))
