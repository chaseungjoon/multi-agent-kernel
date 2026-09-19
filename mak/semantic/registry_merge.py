"""Commit-time reconciliation of a keyed registrar target (Wave 20, P5).

Appenders to a keyed registrar hold it INTENT_WRITE, which they co-hold, so
several can be in flight at once — each agent returning the table as *it* read
it plus its own lines. Committing any of those as-is would drop every line
another appender committed in the meantime: a lost update. So the kernel
replays instead of overwriting: it extracts what the agent *appended* to the
version it read and merges exactly that onto the version committed now.

Only a pure append of keyed entries is replayed. Anything else — an edited or
removed entry, an unkeyed (order-sensitive) entry, a table that stopped being a
registrar — needs the table exclusively, and the caller must upgrade to WRITE
or send the task back.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mak.node_store.registrar import (
    Entry,
    RegistrarKind,
    appended_entries,
    classify,
    merge_append,
)


class MergeKind(StrEnum):
    """What the commit may do with a registrar target."""

    MERGED = "merged"  # ``source`` is the staged edit replayed onto the current table
    EXCLUSIVE = "exclusive"  # not a keyed pure append: needs the node exclusively


@dataclass(frozen=True, slots=True)
class MergePlan:
    """The outcome of reconciling one registrar target."""

    kind: MergeKind
    source: str | None = None
    appended: tuple[Entry, ...] = ()
    reason: str = ""


def plan_merge(
    read_source: str | None, staged_source: str, current_source: str | None
) -> MergePlan:
    """Decide how ``staged_source`` (built on ``read_source``) may be committed."""
    if read_source is None or current_source is None:
        return MergePlan(
            MergeKind.EXCLUSIVE, reason="the table's read version is unknown"
        )
    appended = appended_entries(read_source, staged_source)
    if appended is None:
        return MergePlan(
            MergeKind.EXCLUSIVE, reason="the edit is not a pure append to the table"
        )
    if any(entry.key is None for entry in appended):
        return MergePlan(
            MergeKind.EXCLUSIVE,
            reason="an appended entry has no literal key, so its position matters",
        )
    if classify(current_source) not in (RegistrarKind.KEYED, RegistrarKind.EMPTY):
        return MergePlan(
            MergeKind.EXCLUSIVE, reason="the committed table is no longer keyed"
        )
    merged = merge_append(current_source, appended)
    if merged is None:
        return MergePlan(
            MergeKind.EXCLUSIVE, reason="the append does not replay onto the table"
        )
    return MergePlan(MergeKind.MERGED, source=merged, appended=tuple(appended))
