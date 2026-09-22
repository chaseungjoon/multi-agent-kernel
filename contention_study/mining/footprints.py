"""Load each PR's write footprint out of the cache as set-algebra-ready records.

Separating this from the analyses keeps one definition of "what a change writes
to" — including which paths are excluded as generated — shared by the pair,
window, profile and semantic stages.
"""

from __future__ import annotations

from dataclasses import dataclass

from mining.cache import CacheHandle
from mining.filters import is_generated
from mining.records import parse_ts


@dataclass(frozen=True, slots=True)
class Footprint:
    """One PR's write set at both granularities, plus its lifetime."""

    number: int
    created_at: str
    merged_at: str
    head_sha: str
    fork_point: str
    fork_date: str
    paths: frozenset[str]
    paths_py: frozenset[str]
    nodes: frozenset[str]
    nodes_py: frozenset[str]
    append_nodes: frozenset[str]

    @property
    def fork_ts(self) -> float:
        """Commit time of the PR's fork point, as a POSIX timestamp.

        This, not ``created_at``, is when the change's view of the codebase was
        taken: GitHub reports the *final* head after every rebase and
        force-push, so a long-lived PR's effective base can be far newer than
        the day it was opened.
        """
        return parse_ts(self.fork_date).timestamp() if self.fork_date else 0.0

    @property
    def created_ts(self) -> float:
        """PR creation time as a POSIX timestamp."""
        return parse_ts(self.created_at).timestamp()

    @property
    def merged_ts(self) -> float:
        """PR merge time as a POSIX timestamp."""
        return parse_ts(self.merged_at).timestamp()


def load_footprints(
    handle: CacheHandle, *, bucket: str = "kept", merged_only: bool = True
) -> list[Footprint]:
    """Read the footprints of every PR in one bucket, oldest merge first.

    Generated paths are excluded here and only here, so every downstream
    statistic uses the same exclusion set.
    """
    merged_clause = (
        " AND pr.merged = 1 AND pr.merged_at IS NOT NULL" if merged_only else ""
    )
    rows = handle.conn.execute(
        "SELECT pr.number, pr.created_at, pr.merged_at, pr.closed_at,"
        "       ch.head_sha, ch.fork_point, ch.fork_date"
        " FROM pull_request pr JOIN pr_change ch ON ch.number = pr.number"
        f" WHERE ch.bucket = ?{merged_clause}",
        (bucket,),
    ).fetchall()

    node_rows = handle.conn.execute(
        "SELECT nd.number, nd.path, nd.node_id, nd.kind, nd.is_python, nd.append_only"
        " FROM pr_node nd JOIN pr_change ch ON ch.number = nd.number"
        " WHERE ch.bucket = ?",
        (bucket,),
    ).fetchall()

    paths: dict[int, set[str]] = {}
    paths_py: dict[int, set[str]] = {}
    nodes: dict[int, set[str]] = {}
    nodes_py: dict[int, set[str]] = {}
    appends: dict[int, set[str]] = {}
    for row in node_rows:
        path = str(row["path"])
        if is_generated(path):
            continue
        number = int(row["number"])
        paths.setdefault(number, set()).add(path)
        nodes.setdefault(number, set()).add(str(row["node_id"]))
        if int(row["is_python"]):
            paths_py.setdefault(number, set()).add(path)
            if str(row["kind"]) != "file":
                nodes_py.setdefault(number, set()).add(str(row["node_id"]))
        if int(row["append_only"]):
            appends.setdefault(number, set()).add(str(row["node_id"]))

    footprints: list[Footprint] = []
    for row in rows:
        number = int(row["number"])
        merged_at = row["merged_at"] or row["closed_at"] or row["created_at"]
        footprints.append(
            Footprint(
                number=number,
                created_at=str(row["created_at"]),
                merged_at=str(merged_at),
                head_sha=str(row["head_sha"]),
                fork_point=str(row["fork_point"]),
                fork_date=str(row["fork_date"]),
                paths=frozenset(paths.get(number, ())),
                paths_py=frozenset(paths_py.get(number, ())),
                nodes=frozenset(nodes.get(number, ())),
                nodes_py=frozenset(nodes_py.get(number, ())),
                append_nodes=frozenset(appends.get(number, ())),
            )
        )
    footprints.sort(key=lambda f: (f.merged_at, f.number))
    return footprints
