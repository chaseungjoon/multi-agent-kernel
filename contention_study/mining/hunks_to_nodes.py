"""Stage 23.2 — turn each PR's diff into the set of MAK nodes it writes to.

This is the heart of the study. A change is represented the way the kernel would
represent it: as write locks on AST nodes, not on files. Both sides of every hunk
are mapped — removed lines against the *base* decomposition, added lines against
the *head* decomposition — so a node that a PR creates is a new node id rather
than a write to whatever happened to be at those lines before.

Files the kernel cannot decompose (non-Python, binary, unparseable) are not
discarded: they become a single whole-file node, which is exactly the granularity
MAK has for them today, and they are counted separately in the write-up.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

from mining.cache import CacheHandle, open_cache, set_meta
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.diff_parse import FileDiff, parse_diff
from mining.git_repo import GitRepo
from mining.node_map import empty_index, index_file, whole_file_node_id
from mining.records import ChangeSummary, NodeTouch, PullRequestRecord
from mining.selection import merged_slice, selected_numbers, unmerged_slice

# Beyond this many changed files a PR is a sweep, not a task: it is mapped at
# file level only (node parsing would cost minutes) and bucketed out downstream.
NODE_MAP_FILE_CAP = 600


def _file_level_touch(diff: FileDiff) -> NodeTouch:
    """Represent a whole file as one lockable node."""
    added = sum(hunk.new_count for hunk in diff.hunks)
    removed = sum(hunk.old_count for hunk in diff.hunks)
    if diff.new_path is None:
        change_type = "delete"
    elif diff.old_path is None:
        change_type = "add"
    else:
        change_type = "modify"
    return NodeTouch(
        path=diff.path,
        node_id=whole_file_node_id(diff.path),
        kind="file",
        change_type=change_type,
        lines_added=added,
        lines_removed=removed,
        append_only=removed == 0 and change_type == "modify",
        is_python=diff.path.endswith(".py"),
    )


def map_file_diff(
    repo: GitRepo, fork: str, head: str, diff: FileDiff
) -> tuple[tuple[NodeTouch, ...], bool]:
    """Map one file's hunks onto node touches; also report a parse failure.

    Renamed files are keyed by their **new** path on both sides so a rename does
    not register as a whole file deleted plus a whole file created.
    """
    path = diff.path
    if diff.binary or not path.endswith(".py"):
        return (_file_level_touch(diff),), False

    base_source = repo.blob(fork, diff.old_path) if diff.old_path else None
    head_source = repo.blob(head, diff.new_path) if diff.new_path else None
    base_index = (
        index_file(path, base_source) if base_source is not None else empty_index(path)
    )
    head_index = (
        index_file(path, head_source) if head_source is not None else empty_index(path)
    )
    parse_failed = (base_source is not None and not base_index.parsed) or (
        head_source is not None and not head_index.parsed
    )

    added: dict[str, int] = defaultdict(int)
    removed: dict[str, int] = defaultdict(int)
    kinds: dict[str, str] = {}

    for hunk in diff.hunks:
        if hunk.old_count > 0:
            first, last = hunk.old_start, hunk.old_start + hunk.old_count - 1
            for span in base_index.nodes_in(first, last):
                overlap = min(last, span.end) - max(first, span.start) + 1
                removed[span.node_id] += overlap
                kinds[span.node_id] = span.kind
        if hunk.new_count > 0:
            first, last = hunk.new_start, hunk.new_start + hunk.new_count - 1
            spans = head_index.nodes_in(first, last)
            if not spans and hunk.old_count == 0:
                # Insertion landing in a whitespace-only gap that ingestion drops:
                # attribute it to the node it was inserted after, not to nothing.
                anchor = base_index.node_at(max(1, hunk.old_start))
                spans = (anchor,) if anchor is not None else ()
            for span in spans:
                added[span.node_id] += min(last, span.end) - max(first, span.start) + 1
                kinds[span.node_id] = span.kind

    base_ids, head_ids = base_index.node_ids, head_index.node_ids
    touches: list[NodeTouch] = []
    for node_id in sorted(set(added) | set(removed)):
        if node_id in head_ids and node_id not in base_ids:
            change_type = "add"
        elif node_id in base_ids and node_id not in head_ids:
            change_type = "delete"
        else:
            change_type = "modify"
        lines_removed = removed.get(node_id, 0)
        touches.append(
            NodeTouch(
                path=path,
                node_id=node_id,
                kind=kinds.get(node_id, "unknown"),
                change_type=change_type,
                lines_added=added.get(node_id, 0),
                lines_removed=lines_removed,
                append_only=lines_removed == 0 and change_type == "modify",
                is_python=True,
            )
        )
    if not touches and diff.hunks:
        # A diff with hunks but no mapped node (e.g. trailing-newline-only change)
        # still represents a write; keep it at file granularity.
        touches.append(_file_level_touch(diff))
    return tuple(touches), parse_failed


def analyze_change(
    repo: GitRepo, record: PullRequestRecord, main_sha: str
) -> ChangeSummary:
    """Compute the node-level footprint of one PR against its fork point."""
    empty = ChangeSummary(
        number=record.number, fork_point="", fork_date="",
        head_sha=record.head_sha, status="unknown", files_total=0, files_python=0,
        lines_added=0, lines_removed=0, parse_failures=0,
    )
    if not repo.has_object(record.head_sha):
        return replace(empty, status="no_head")

    fork = None
    if repo.has_object(record.base_sha):
        fork = repo.merge_base(record.head_sha, record.base_sha)
    if fork is None or fork == record.head_sha:
        fork = repo.merge_base(record.head_sha, main_sha)
    if fork is None or fork == record.head_sha:
        return replace(empty, status="no_base")

    diffs = parse_diff(repo.diff_u0(fork, record.head_sha))
    files_python = sum(1 for d in diffs if d.path.endswith(".py"))
    lines_added = sum(h.new_count for d in diffs for h in d.hunks)
    lines_removed = sum(h.old_count for d in diffs for h in d.hunks)

    if len(diffs) > NODE_MAP_FILE_CAP:
        touches = tuple(_file_level_touch(d) for d in diffs)
        status = "oversize_file_level"
        parse_failures = 0
    else:
        touches = ()
        parse_failures = 0
        collected: list[NodeTouch] = []
        for diff in diffs:
            mapped, failed = map_file_diff(repo, fork, record.head_sha, diff)
            collected.extend(mapped)
            parse_failures += int(failed)
        touches = tuple(collected)
        status = "ok"

    return ChangeSummary(
        number=record.number,
        fork_point=fork,
        fork_date=repo.text("show", "-s", "--format=%cI", fork).strip(),
        head_sha=record.head_sha,
        status=status,
        files_total=len(diffs),
        files_python=files_python,
        lines_added=lines_added,
        lines_removed=lines_removed,
        parse_failures=parse_failures,
        touches=touches,
    )


_WORKER_REPO: GitRepo | None = None


def _worker_init(git_dir: str) -> None:
    """Open the bare clone once per pool worker."""
    global _WORKER_REPO
    _WORKER_REPO = GitRepo(Path(git_dir))


def _worker_map(payload: tuple[PullRequestRecord, str]) -> ChangeSummary:
    """Pool entry point: map one PR (the repo handle is process-local)."""
    record, main_sha = payload
    if _WORKER_REPO is None:
        raise RuntimeError("worker was not initialised with a repository")
    return analyze_change(_WORKER_REPO, record, main_sha)


def _store(handle: CacheHandle, summaries: list[ChangeSummary]) -> None:
    """Persist a batch of change summaries and their node touches."""
    handle.conn.executemany(
        "INSERT INTO pr_change (number, fork_point, fork_date, head_sha, status,"
        " files_total, files_python, lines_added, lines_removed, nodes_total,"
        " nodes_python, parse_failures, bucket) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(number) DO UPDATE SET fork_point=excluded.fork_point,"
        " fork_date=excluded.fork_date, status=excluded.status,"
        " nodes_total=excluded.nodes_total",
        [
            (
                s.number, s.fork_point, s.fork_date, s.head_sha, s.status,
                s.files_total,
                s.files_python, s.lines_added, s.lines_removed,
                len(s.node_ids), len({t.node_id for t in s.touches if t.is_python}),
                s.parse_failures, "unclassified",
            )
            for s in summaries
        ],
    )
    handle.conn.executemany(
        "INSERT OR REPLACE INTO pr_node (number, path, node_id, kind, change_type,"
        " lines_added, lines_removed, append_only, is_python)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                s.number, t.path, t.node_id, t.kind, t.change_type,
                t.lines_added, t.lines_removed, int(t.append_only), int(t.is_python),
            )
            for s in summaries
            for t in s.touches
        ],
    )
    handle.conn.commit()


def build_changes(
    config: StudyConfig, spec: RepoSpec, *, verbose: bool = True
) -> int:
    """Map every selected PR of one repository; return how many were mapped."""
    handle = open_cache(config, spec)
    repo = GitRepo(config.clone_dir(spec))
    main_sha = repo.text("rev-parse", f"refs/heads/{spec.main_branch}").strip()

    wanted = set(selected_numbers(handle, config, include_unmerged=True))
    done = {
        int(row["number"])
        for row in handle.conn.execute("SELECT number FROM pr_change").fetchall()
    }
    todo_numbers = sorted(wanted - done)
    records = [
        record
        for record in _all_selected_records(handle, config)
        if record.number in set(todo_numbers)
    ]
    if verbose:
        print(
            f"  {spec.slug}: {len(records)} PRs to map ({len(done)} cached)",
            flush=True,
        )

    batch: list[ChangeSummary] = []
    mapped = 0
    if records:
        with ProcessPoolExecutor(
            max_workers=config.workers,
            initializer=_worker_init,
            initargs=(str(config.clone_dir(spec)),),
        ) as pool:
            payloads = [(record, main_sha) for record in records]
            for summary in pool.map(_worker_map, payloads, chunksize=4):
                batch.append(summary)
                mapped += 1
                if len(batch) >= 200:
                    _store(handle, batch)
                    batch = []
                    if verbose:
                        print(f"    mapped {mapped}/{len(records)}", flush=True)
    if batch:
        _store(handle, batch)
    set_meta(handle, "node_map_file_cap", str(NODE_MAP_FILE_CAP))
    handle.conn.close()
    return mapped


def _all_selected_records(
    handle: CacheHandle, config: StudyConfig
) -> list[PullRequestRecord]:
    """Return the merged slice plus its closed-unmerged control group."""
    merged = merged_slice(handle, config)
    if not merged:
        return []
    unmerged = unmerged_slice(
        handle, config, merged[0].created_at, merged[-1].merged_at or config.until
    )
    return merged + unmerged


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.hunks_to_nodes [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        print(f"mapping {spec.slug} ...", flush=True)
        count = build_changes(config, spec)
        print(f"  -> {count} PRs mapped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
