"""Classify a stale read and decide what the commit does about it (Wave 20, D1).

A *stale read* is a node a task's bundle carried that someone else committed
while the task was in flight (see :mod:`mak.semantic.read_set`). Most are
harmless — a sibling function's body changed and the task never called it — and
a few are exactly the bug this wave exists for: a signature changed under a new
call, a return type changed under code that indexes the result. The kernel's
job is to tell those apart *cheaply and conservatively*: accept when it can
prove the task's output does not depend on what changed, re-verify when the
static checks can, and send the task back with the diff otherwise.

Every decision is per node and explainable; the commit's verdict is the most
severe of them (reject > redispatch > accept).
"""

from __future__ import annotations

import ast
import difflib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from mak.core.types import NodeId
from mak.node_store.api_digest import api_fingerprint
from mak.semantic.interface import changed_bindings, short_names
from mak.semantic.read_set import ReadMark
from mak.semantic.symbols import bound_names


class ChangeKind(StrEnum):
    """What happened to a node between dispatch and commit."""

    BODY_ONLY = "body_only"  # interface fingerprint unchanged
    API_CHANGE = "api_change"  # interface changed
    DELETED = "deleted"  # the node is gone
    CREATED = "created"  # it did not exist at dispatch and does now


class Verdict(StrEnum):
    """What the commit does, ordered by severity."""

    ACCEPT = "accept"
    REDISPATCH = "redispatch"
    REJECT = "reject"


_SEVERITY = {Verdict.ACCEPT: 0, Verdict.REDISPATCH: 1, Verdict.REJECT: 2}


@dataclass(frozen=True, slots=True)
class StaleRead:
    """One node that changed underneath a task, classified."""

    node_id: NodeId
    mark: ReadMark
    current_version: int | None
    current_source: str | None
    kind: ChangeKind
    # The interface change is confined to parameter lists of module-level
    # functions — the one kind the static checks can fully re-verify.
    shape_only: bool
    # The task's staged code names something the node defined.
    referenced: bool


@dataclass(frozen=True, slots=True)
class NodeDecision:
    """The verdict for one stale node, and why."""

    stale: StaleRead
    verdict: Verdict
    reason: str


@dataclass(frozen=True, slots=True)
class StaleDecision:
    """The verdict for a whole commit."""

    verdict: Verdict
    nodes: tuple[NodeDecision, ...]


def classify(
    mark: ReadMark,
    current_version: int | None,
    current_source: str | None,
    staged_sources: Mapping[NodeId, str],
) -> StaleRead:
    """Classify how the node changed and whether the staged code depends on it."""
    old = mark.source
    if current_source is None:
        kind = ChangeKind.DELETED
    elif mark.digest is None:
        kind = ChangeKind.CREATED
    elif old is None:
        # Restored from disk without its source: nothing to compare against,
        # so the change cannot be proven body-only.
        kind = ChangeKind.API_CHANGE
    else:
        # Body-only means no *existing* binding changed: a node that only gained
        # a name (a new import, a new helper, a new method) broke nobody.
        changed = changed_bindings(old, current_source)
        kind = ChangeKind.BODY_ONLY if changed == set() else ChangeKind.API_CHANGE
    shape_only = (
        kind is ChangeKind.API_CHANGE
        and old is not None
        and current_source is not None
        and _shape_only(old, current_source)
    )
    return StaleRead(
        node_id=mark.node_id,
        mark=mark,
        current_version=current_version,
        current_source=current_source,
        kind=kind,
        shape_only=shape_only,
        referenced=_referenced(mark.node_id, old, current_source, staged_sources),
    )


def decide(
    policy: str,
    stale_reads: list[StaleRead],
    *,
    recheck: Callable[[list[StaleRead]], list[str]],
    adjudicate: Callable[[StaleRead], bool | None] | None = None,
) -> StaleDecision:
    """Apply ``policy`` to every stale read; see ``SemanticConfig.

    stale_read``.

    ``recheck`` re-runs the static checks for the task's staged code against
    the *current* code of the given nodes and returns any defects. It is only
    called when a verdict depends on it, and at most once per commit.
    ``adjudicate`` answers an uncertain case (True = still holds); None or an
    unsure answer falls back to re-dispatch, never to acceptance.
    """
    recheck_cache: list[list[str]] = []

    def defects() -> list[str]:
        if not recheck_cache:
            recheck_cache.append(
                recheck([s for s in stale_reads if s.kind is ChangeKind.API_CHANGE])
            )
        return recheck_cache[0]

    decisions = tuple(
        _decide_one(policy, stale, defects, adjudicate) for stale in stale_reads
    )
    verdict = max(
        (d.verdict for d in decisions),
        key=_SEVERITY.__getitem__,
        default=Verdict.ACCEPT,
    )
    return StaleDecision(verdict, decisions)


def _decide_one(
    policy: str,
    stale: StaleRead,
    defects: Callable[[], list[str]],
    adjudicate: Callable[[StaleRead], bool | None] | None,
) -> NodeDecision:
    if stale.kind is ChangeKind.BODY_ONLY and stale.mark.api_only:
        return NodeDecision(
            stale, Verdict.ACCEPT, "only the interface was shown, and it is unchanged"
        )
    if policy == "redispatch":
        return NodeDecision(
            stale, Verdict.REDISPATCH, "strict snapshot isolation: any change re-runs"
        )
    if stale.kind is ChangeKind.BODY_ONLY:
        return NodeDecision(stale, Verdict.ACCEPT, "body-only change")
    if policy == "reject":
        return NodeDecision(stale, Verdict.REJECT, f"{stale.kind} under policy reject")
    if policy == "accept_if_api_stable":
        return NodeDecision(stale, Verdict.REDISPATCH, f"{stale.kind}")
    return _revalidate(stale, defects, adjudicate)


def _revalidate(
    stale: StaleRead,
    defects: Callable[[], list[str]],
    adjudicate: Callable[[StaleRead], bool | None] | None,
) -> NodeDecision:
    """Apply the default policy: accept what is provably safe, else re-run."""
    if not stale.referenced:
        return NodeDecision(
            stale, Verdict.ACCEPT, "the task's code uses nothing whose binding changed"
        )
    if stale.kind is ChangeKind.API_CHANGE:
        found = defects()
        if found:
            return NodeDecision(
                stale, Verdict.REDISPATCH, "re-checked against the new code: "
                + "; ".join(found)
            )
        if stale.shape_only:
            return NodeDecision(
                stale, Verdict.ACCEPT,
                "parameter-shape change re-verified against the new code",
            )
    if adjudicate is not None and adjudicate(stale) is True:
        return NodeDecision(
            stale, Verdict.ACCEPT, "adjudicated: the task's use still holds"
        )
    return NodeDecision(
        stale, Verdict.REDISPATCH,
        f"{stale.kind} the static checks cannot settle",
    )


def _shape_only(old: str, new: str) -> bool:
    """Whether only module-level functions' parameter lists differ."""
    if api_fingerprint(old, parameters=False) != api_fingerprint(
        new, parameters=False
    ):
        return False
    old_params, new_params = _param_lists(old), _param_lists(new)
    if old_params is None or new_params is None:
        return False
    changed = {
        name for name in old_params.keys() | new_params.keys()
        if old_params.get(name) != new_params.get(name)
    }
    return all("." not in name for name in changed)


def _param_lists(source: str) -> dict[str, str] | None:
    """``{qualname: rendered parameters}`` for every function in ``source``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    found: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            found[stmt.name] = ast.unparse(stmt.args)
        elif isinstance(stmt, ast.ClassDef):
            for member in stmt.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    found[f"{stmt.name}.{member.name}"] = ast.unparse(member.args)
    return found


def _referenced(
    node_id: NodeId,
    old: str | None,
    new: str | None,
    staged_sources: Mapping[NodeId, str],
) -> bool:
    """Whether the staged code names something whose binding changed.

    Only names that were *removed or re-bound* count (see
    :func:`mak.semantic.interface.changed_bindings`). A sibling task adding an
    import to the shared header, or a new helper beside the task's function,
    changes the node without changing anything the task could have relied on.
    With nothing to read names from, the node counts as referenced — "unknown"
    must never read as "safe".
    """
    if old is None:
        names = bound_names(new) if new is not None else None
    else:
        changed = changed_bindings(old, new)
        names = short_names(changed) if changed is not None else None
    if names is None:
        return True
    if old is not None and new is None:
        names |= _id_names(node_id)
    names = {n for n in names if n and not (n.startswith("__") and n.endswith("__"))}
    if not names:
        return False
    text = "\n".join(staged_sources.values())
    return any(re.search(rf"\b{re.escape(name)}\b", text) for name in names)


def _id_names(node_id: NodeId) -> set[str]:
    """Return the symbol (and owning class) a ``file::kind::name`` id names."""
    parts = str(node_id).split("::")
    if len(parts) < 3:
        return set()
    name = parts[2].split("#", 1)[0]
    return {name.rsplit(".", 1)[-1], name.split(".", 1)[0]}


def retry_note(decision: StaleDecision, *, limit: int = 6000) -> str:
    """Return the R1 retry note: why the attempt was sent back, with diffs.

    Bounded: each diff is shown in turn until ``limit`` characters are spent,
    and the rest are named without their bodies, so one sprawling change cannot
    push the task's own context out of the model's window.
    """
    blocking = [d for d in decision.nodes if d.verdict is not Verdict.ACCEPT]
    header = (
        "Your previous attempt was built on code that another task changed "
        "while you were working, so it was not committed. Redo the task "
        "against the CURRENT code — your bundle has been refreshed — and "
        "adapt every use of what changed. What changed since you read it:"
    )
    parts = [header]
    spent = len(header)
    for node in blocking:
        diff = _diff(node.stale)
        entry = f"\n\n{node.stale.node_id} ({node.stale.kind}; {node.reason}):\n{diff}"
        if spent + len(entry) > limit:
            parts.append(
                f"\n\n{node.stale.node_id} ({node.stale.kind}) — diff omitted "
                "for length; read its current source in your bundle."
            )
            continue
        parts.append(entry)
        spent += len(entry)
    return "".join(parts)


def _diff(stale: StaleRead) -> str:
    old = (stale.mark.source or "").splitlines(keepends=True)
    new = (stale.current_source or "").splitlines(keepends=True)
    version = stale.mark.version if stale.mark.version is not None else "absent"
    current = stale.current_version if stale.current_version is not None else "gone"
    lines = difflib.unified_diff(
        old, new, fromfile=f"read (v{version})", tofile=f"now (v{current})"
    )
    text = "".join(lines)
    return text if text else "(no textual difference recorded)"
