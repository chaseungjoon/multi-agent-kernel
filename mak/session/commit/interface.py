"""Hold every interface change to the task's declaration and its locks."""

from __future__ import annotations

from mak.core.logging import EventType
from mak.core.types import LockMode, NodeId
from mak.lock_manager.resources import api_resource
from mak.node_store.api_digest import api_fingerprint
from mak.semantic.interface import changed_bindings
from mak.session.commit.verdict import (
    ACCEPT,
    CommitContext,
    Verdict,
    defer,
    resend,
)


class InterfaceGranted:
    """An interface change needs a declaration that allows it and ``#api`` WRITE.

    A task that declared ``changes_api=False`` promised a body-only edit —
    callers' tasks ran beside it on the strength of that — so an interface
    change is refused. Any other interface change needs WRITE on
    ``node#api``; one the task was not granted is taken now if it is free, and
    waits when a concurrent task is building against the interface.
    """

    name = "interface_granted"

    def __init__(self, *, api_locks: bool) -> None:
        self._api_locks = api_locks

    def check(self, ctx: CommitContext) -> Verdict:
        """Refuse a broken promise; take or wait for the interface lock."""
        if not self._api_locks:
            return ACCEPT
        for node_id in ctx.staged:
            change = _interface_change(ctx, node_id)
            if change is None:
                continue
            if ctx.task.changes_api is False:
                ctx.log(
                    EventType.API_ESCALATED, task_id=ctx.task_id, node_id=str(node_id),
                    outcome="refused_promise", change=change,
                )
                return _broken_promise(node_id, change)
            verdict = _hold_interface(ctx, node_id, change)
            if verdict is not None:
                return verdict
        return ACCEPT


def _broken_promise(node_id: NodeId, change: str) -> Verdict:
    """Send back a body-only task that changed an interface."""
    return resend(
        [f"body-only task changed the interface of '{node_id}'"],
        note=(
            "This task was planned as a body-only change, so other "
            f"tasks are relying on the interface of '{node_id}' "
            f"staying exactly as it is. Your version changed it "
            f"({change}). Keep every signature, parameter, return "
            "annotation, decorator, class field and import exactly "
            "as they are; change only function bodies."
        ),
        error_kind="undeclared_api_change",
    )


def _interface_change(ctx: CommitContext, node_id: NodeId) -> str | None:
    """Describe how the staged source changes a node's interface, or None.

    A node that does not exist yet has no interface to change — nothing can
    have been built against it — and a node that only *gained* a binding
    (a new import, a new helper) broke nobody.
    """
    fragment = ctx.view.store.get_staged(node_id)
    old = ctx.view.dependency_source(node_id)
    if fragment is None or old is None:
        return None
    changed = changed_bindings(old, fragment.source)
    if changed == set():
        return None
    return describe_interface_change(
        api_fingerprint(old), api_fingerprint(fragment.source), changed
    )


def _hold_interface(
    ctx: CommitContext, node_id: NodeId, change: str
) -> Verdict | None:
    """Make sure the task holds WRITE on every interface it is changing.

    Returns None when it does (or just took it), else the verdict to wait.
    """
    resources = [
        api_resource(n) for n in (node_id, *ctx.view.file_fragment_ids(node_id))
    ]
    needed = [
        (r, LockMode.WRITE) for r in resources
        if not ctx.lock_table.holds_all([(r, LockMode.WRITE)], ctx.task_id)
    ]
    if not needed:
        return None
    if ctx.escalate(needed):
        ctx.log(
            EventType.API_ESCALATED, task_id=ctx.task_id, node_id=str(node_id),
            outcome="acquired", change=change,
        )
        return None
    readers = sorted({
        entry.holder
        for resource, _ in needed
        for entry in ctx.lock_table.all_entries().get(resource, [])
        if entry.holder != ctx.task_id
    })
    ctx.log(
        EventType.API_ESCALATED, task_id=ctx.task_id, node_id=str(node_id),
        outcome="refused_contended", change=change, readers=readers,
    )
    # The readers will validate their own commits against whatever this
    # one leaves behind; this commit only has to wait until none of them
    # is still building against the old interface.
    return defer(
        f"undeclared interface change to '{node_id}' waits for "
        f"{', '.join(readers)}, which depend on its current interface"
    )


def describe_interface_change(
    before: str | None, after: str | None, changed: set[str] | None
) -> str:
    """Return a short, readable account of an interface change."""
    if before is None or after is None or changed is None:
        return "its interface could not be read"
    old, new = set(before.splitlines()), set(after.splitlines())
    removed = [line.strip() for line in before.splitlines() if line not in new]
    added = [line.strip() for line in after.splitlines() if line not in old]
    parts = []
    if removed:
        parts.append("was: " + "; ".join(removed[:3]))
    if added:
        parts.append("now: " + "; ".join(added[:3]))
    names = ", ".join(sorted(changed)[:5])
    return f"{names}: " + (" / ".join(parts) or "its interface changed")
