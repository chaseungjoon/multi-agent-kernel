"""Typed records exchanged between pipeline stages.

Every cross-module value is one of these frozen dataclasses rather than a dict,
so a stage's contract is visible in its signature and a schema change is a type
error instead of a silent ``KeyError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PullRequestRecord:
    """PR metadata as returned by the GitHub list endpoint.

    Timestamps stay in their original ISO-8601 UTC form; :func:`parse_ts` turns
    them into datetimes at the point of use.
    """

    number: int
    created_at: str
    merged_at: str | None
    closed_at: str | None
    merged: bool
    base_ref: str
    base_sha: str
    head_sha: str
    user_login: str
    user_type: str
    draft: bool


@dataclass(frozen=True, slots=True)
class Hunk:
    """One ``@@`` range from ``git diff -U0``, in *old-file* coordinates.

    ``old_start`` is 1-indexed. ``old_count`` is 0 for a pure insertion, in which
    case the new lines land immediately *after* old line ``old_start``.
    """

    path: str
    old_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass(frozen=True, slots=True)
class NodeTouch:
    """One AST node (or whole non-Python file) that a change writes to."""

    path: str
    node_id: str
    kind: str
    change_type: str
    lines_added: int
    lines_removed: int
    append_only: bool
    is_python: bool


@dataclass(frozen=True, slots=True)
class ChangeSummary:
    """Everything one PR's diff contributes to the study."""

    number: int
    fork_point: str
    fork_date: str
    head_sha: str
    status: str
    files_total: int
    files_python: int
    lines_added: int
    lines_removed: int
    parse_failures: int
    touches: tuple[NodeTouch, ...] = field(default_factory=tuple)

    @property
    def paths(self) -> frozenset[str]:
        """Distinct paths this change writes to."""
        return frozenset(touch.path for touch in self.touches)

    @property
    def node_ids(self) -> frozenset[str]:
        """Distinct node ids this change writes to."""
        return frozenset(touch.node_id for touch in self.touches)


@dataclass(frozen=True, slots=True)
class PairRow:
    """One concurrent PR pair, classified into the study's 2x2."""

    a: int
    b: int
    overlap_seconds: int
    files_a: int
    files_b: int
    files_shared: int
    py_files_shared: int
    nodes_shared: int
    py_nodes_shared: int
    shared_all_append: bool
    merge_verdict: str
    conflict_files: int
    conflict_py_files: int
    conflict_paths: str


@dataclass(frozen=True, slots=True)
class WindowRow:
    """Contention statistics for one k-window replay."""

    k: int
    window_index: int
    first_pr: int
    distinct_files: int
    contended_files: int
    distinct_nodes: int
    contended_nodes: int
    max_file_chain: int
    max_node_chain: int
    any_file_collision: bool
    any_node_collision: bool
    distinct_py_files: int
    contended_py_files: int
    distinct_py_nodes: int
    contended_py_nodes: int
    max_py_file_chain: int
    max_py_node_chain: int
    any_py_file_collision: bool
    any_py_node_collision: bool


@dataclass(frozen=True, slots=True)
class SemanticRow:
    """RQ6: static defects present in a merge but in neither side alone."""

    a: int
    b: int
    status: str
    base_defects: int
    a_defects: int
    b_defects: int
    merge_defects: int
    new_defects: int
    new_kinds: str
    detail: str


def parse_ts(value: str) -> datetime:
    """Parse a GitHub ISO-8601 UTC timestamp (``...Z``) into a datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
