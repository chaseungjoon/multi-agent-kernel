"""Stage 23.8 — export the derived dataset other people can check the study with.

What is released is *derived* data only: one row per analysed pair, one row per
window, and node write-frequency counts. No source code is redistributed, and no
author identity beyond an automation flag — the PR number is already public, and
nothing here adds a name, an email address or a login to it.
"""

from __future__ import annotations

import csv
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from mining.cache import CacheHandle, open_cache
from mining.cli import chosen, repo_parser
from mining.config import RepoSpec, StudyConfig, load_config
from mining.filters import is_generated
from mining.hot_nodes import categorise

_CHANGE_QUERY = """
SELECT ch.number, ch.status, ch.bucket, ch.files_total, ch.files_python,
       ch.lines_added, ch.lines_removed, ch.nodes_total, ch.nodes_python,
       ch.parse_failures, pr.created_at, pr.merged_at, ch.fork_date,
       (pr.user_type = 'Bot') AS automated
FROM pr_change ch JOIN pull_request pr ON pr.number = ch.number
ORDER BY ch.number
"""

_PAIR_QUERY = """
SELECT a, b, overlap_seconds, files_a, files_b, files_shared, py_files_shared,
       nodes_shared, py_nodes_shared, shared_all_append, merge_verdict,
       conflict_files, conflict_py_files
FROM pair ORDER BY a, b
"""

_WINDOW_QUERY = """
SELECT k, window_index, first_pr, distinct_files, contended_files,
       distinct_nodes, contended_nodes, max_file_chain, max_node_chain,
       any_file_collision, any_node_collision, distinct_py_files,
       contended_py_files, distinct_py_nodes, contended_py_nodes,
       max_py_file_chain, max_py_node_chain, any_py_file_collision,
       any_py_node_collision
FROM window_row ORDER BY k, window_index
"""

_SEMANTIC_QUERY = """
SELECT a, b, status, base_defects, a_defects, b_defects, merge_defects,
       new_defects, new_kinds
FROM semantic_row ORDER BY a, b
"""


def _write_csv(
    path: Path, header: Sequence[str], rows: Iterable[Sequence[object]]
) -> int:
    """Write one CSV and return how many data rows it holds."""
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def _dump_query(handle: CacheHandle, query: str, path: Path) -> int:
    """Run a query and stream it straight into a CSV with its column names."""
    cursor = handle.conn.execute(query)
    header = [column[0] for column in cursor.description]
    return _write_csv(path, header, cursor.fetchall())


def _dump_node_counts(handle: CacheHandle, path: Path) -> int:
    """Per-node write counts, each tagged with its hot-node category."""
    rows = handle.conn.execute(
        "SELECT nd.node_id, nd.kind, MAX(nd.is_python) AS is_python,"
        "       COUNT(DISTINCT nd.number) AS writes,"
        "       SUM(nd.append_only) AS append_only_touches"
        " FROM pr_node nd JOIN pr_change ch ON ch.number = nd.number"
        " WHERE ch.bucket = 'kept' GROUP BY nd.node_id, nd.kind"
    ).fetchall()
    return _write_csv(
        path,
        ("node_id", "kind", "is_python", "is_generated_path", "category",
         "writes", "append_only_touches"),
        [
            (
                str(row["node_id"]), str(row["kind"]), int(row["is_python"]),
                int(is_generated(str(row["node_id"]).split("::", 1)[0])),
                categorise(str(row["node_id"]), str(row["kind"])),
                int(row["writes"]), int(row["append_only_touches"] or 0),
            )
            for row in rows
        ],
    )


def export(config: StudyConfig, spec: RepoSpec, *, verbose: bool = True) -> Path:
    """Write one repository's release CSVs; return the directory."""
    handle = open_cache(config, spec)
    directory = config.repo_data_dir(spec) / "release"
    directory.mkdir(parents=True, exist_ok=True)
    counts = {
        "changes.csv": _dump_query(handle, _CHANGE_QUERY, directory / "changes.csv"),
        "pairs.csv": _dump_query(handle, _PAIR_QUERY, directory / "pairs.csv"),
        "windows.csv": _dump_query(handle, _WINDOW_QUERY, directory / "windows.csv"),
        "semantic.csv": _dump_query(
            handle, _SEMANTIC_QUERY, directory / "semantic.csv"
        ),
        "node_writes.csv": _dump_node_counts(handle, directory / "node_writes.csv"),
    }
    handle.conn.close()
    if verbose:
        summary = ", ".join(f"{name} {n}" for name, n in counts.items())
        print(f"  {spec.slug}: {summary}", flush=True)
    return directory


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m mining.release [owner/repo ...]``."""
    parser = repo_parser(__doc__)
    args = parser.parse_args(argv)

    config = load_config()
    specs = chosen(config, args.repos)
    for spec in specs:
        export(config, spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
