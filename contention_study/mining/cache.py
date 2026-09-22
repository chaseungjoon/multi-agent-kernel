"""SQLite persistence for every stage of the pipeline.

One database per repository (``data/<repo>/cache.sqlite``) holds PR metadata,
per-PR changed nodes, pair rows, window rows and semantic-probe rows. Each stage
is resumable: it reads what is already present and only computes the rest, which
is what makes a cached re-run cheap.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mining.config import RepoSpec, StudyConfig

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_request (
    number      INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    merged_at   TEXT,
    closed_at   TEXT,
    merged      INTEGER NOT NULL,
    base_ref    TEXT NOT NULL,
    base_sha    TEXT NOT NULL,
    head_sha    TEXT NOT NULL,
    user_login  TEXT NOT NULL,
    user_type   TEXT NOT NULL,
    draft       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pr_merged_at ON pull_request (merged_at);

CREATE TABLE IF NOT EXISTS pr_change (
    number         INTEGER PRIMARY KEY,
    fork_point     TEXT NOT NULL,
    fork_date      TEXT NOT NULL DEFAULT '',
    head_sha       TEXT NOT NULL,
    status         TEXT NOT NULL,
    files_total    INTEGER NOT NULL,
    files_python   INTEGER NOT NULL,
    lines_added    INTEGER NOT NULL,
    lines_removed  INTEGER NOT NULL,
    nodes_total    INTEGER NOT NULL,
    nodes_python   INTEGER NOT NULL,
    parse_failures INTEGER NOT NULL,
    bucket         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_node (
    number        INTEGER NOT NULL,
    path          TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    change_type   TEXT NOT NULL,
    lines_added   INTEGER NOT NULL,
    lines_removed INTEGER NOT NULL,
    append_only   INTEGER NOT NULL,
    is_python     INTEGER NOT NULL,
    PRIMARY KEY (number, node_id)
);
CREATE INDEX IF NOT EXISTS pr_node_by_node ON pr_node (node_id);

CREATE TABLE IF NOT EXISTS pair (
    a                INTEGER NOT NULL,
    b                INTEGER NOT NULL,
    overlap_seconds  INTEGER NOT NULL,
    files_a          INTEGER NOT NULL,
    files_b          INTEGER NOT NULL,
    files_shared     INTEGER NOT NULL,
    py_files_shared  INTEGER NOT NULL,
    nodes_shared     INTEGER NOT NULL,
    py_nodes_shared  INTEGER NOT NULL,
    shared_all_append INTEGER NOT NULL,
    merge_verdict    TEXT NOT NULL,
    conflict_files   INTEGER NOT NULL,
    conflict_py_files INTEGER NOT NULL,
    conflict_paths   TEXT NOT NULL,
    PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS window_row (
    k                 INTEGER NOT NULL,
    window_index      INTEGER NOT NULL,
    first_pr          INTEGER NOT NULL,
    distinct_files    INTEGER NOT NULL,
    contended_files   INTEGER NOT NULL,
    distinct_nodes    INTEGER NOT NULL,
    contended_nodes   INTEGER NOT NULL,
    max_file_chain    INTEGER NOT NULL,
    max_node_chain    INTEGER NOT NULL,
    any_file_collision INTEGER NOT NULL,
    any_node_collision INTEGER NOT NULL,
    distinct_py_files  INTEGER NOT NULL DEFAULT 0,
    contended_py_files INTEGER NOT NULL DEFAULT 0,
    distinct_py_nodes  INTEGER NOT NULL DEFAULT 0,
    contended_py_nodes INTEGER NOT NULL DEFAULT 0,
    max_py_file_chain  INTEGER NOT NULL DEFAULT 0,
    max_py_node_chain  INTEGER NOT NULL DEFAULT 0,
    any_py_file_collision INTEGER NOT NULL DEFAULT 0,
    any_py_node_collision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (k, window_index)
);

CREATE TABLE IF NOT EXISTS semantic_row (
    a            INTEGER NOT NULL,
    b            INTEGER NOT NULL,
    status       TEXT NOT NULL,
    base_defects INTEGER NOT NULL,
    a_defects    INTEGER NOT NULL,
    b_defects    INTEGER NOT NULL,
    merge_defects INTEGER NOT NULL,
    new_defects  INTEGER NOT NULL,
    new_kinds    TEXT NOT NULL,
    detail       TEXT NOT NULL,
    PRIMARY KEY (a, b)
);

CREATE TABLE IF NOT EXISTS audit_row (
    cell       TEXT NOT NULL,
    a          INTEGER NOT NULL,
    b          INTEGER NOT NULL,
    verdict    TEXT NOT NULL,
    detail     TEXT NOT NULL,
    PRIMARY KEY (cell, a, b)
);

CREATE TABLE IF NOT EXISTS run_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class CacheHandle:
    """An open database plus the repository it belongs to."""

    spec: RepoSpec
    path: Path
    conn: sqlite3.Connection


def open_cache(config: StudyConfig, spec: RepoSpec) -> CacheHandle:
    """Open (creating if needed) the SQLite cache for one repository."""
    directory = config.repo_data_dir(spec)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "cache.sqlite"
    conn = sqlite3.connect(path, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # WAL keeps the writer from blocking the readers used by the process pool.
    _migrate(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.commit()
    return CacheHandle(spec=spec, path=path, conn=conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a cache was first created.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, so the existing columns are read
    back and only the missing ones are added. Keeping this explicit means an
    older cache is upgraded rather than silently re-fetched.
    """
    change_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(pr_change)").fetchall()
    }
    if "fork_date" not in change_columns:
        conn.execute(
            "ALTER TABLE pr_change ADD COLUMN fork_date TEXT NOT NULL DEFAULT ''"
        )
    window_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(window_row)").fetchall()
    }
    for column in (
        "distinct_py_files", "contended_py_files", "distinct_py_nodes",
        "contended_py_nodes", "max_py_file_chain", "max_py_node_chain",
        "any_py_file_collision", "any_py_node_collision",
    ):
        if column not in window_columns:
            conn.execute(
                f"ALTER TABLE window_row ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
            )


def set_meta(handle: CacheHandle, key: str, value: str) -> None:
    """Record one reproducibility fact (fetch date, clone SHA, filter counts)."""
    handle.conn.execute(
        "INSERT INTO run_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    handle.conn.commit()


def get_meta(handle: CacheHandle, key: str, default: str = "") -> str:
    """Read back a recorded fact, or ``default`` when it was never written."""
    row = handle.conn.execute(
        "SELECT value FROM run_meta WHERE key = ?", (key,)
    ).fetchone()
    return str(row["value"]) if row is not None else default


def table_count(handle: CacheHandle, table: str) -> int:
    """Row count for one of the pipeline's tables.

    ``table`` is validated against the known schema because it is interpolated
    into SQL; an unknown name is a programming error, not user input.
    """
    known = {
        "pull_request", "pr_change", "pr_node", "pair",
        "window_row", "semantic_row", "audit_row", "run_meta",
    }
    if table not in known:
        raise ValueError(f"unknown table {table!r}")
    row = handle.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])
