"""Grow a commit by what the reply added beside its grant: helpers and imports.

Both checks act on the task's :class:`~mak.session.types.Growth` and stage what
they add as *extras* (:class:`~mak.session.commit.verdict.NewStaging`), so every
later check — staleness, structure, compilation, prospective semantics — judges
the commit whole. The locks they take are transient: they guard this one
commit and are given back when it ends, committed or not.
"""

from __future__ import annotations

import sys

from mak.core.logging import EventType
from mak.core.types import LockMode, NodeId, SubTask
from mak.lock_manager.resources import import_name_resource, imports_resource
from mak.scheduler.lock_policy import lock_requests
from mak.semantic.import_merge import (
    ImportMergePlan,
    merge_imports,
    statement_bindings,
)
from mak.session.commit.verdict import (
    ACCEPT,
    CommitContext,
    NewStaging,
    Verdict,
    accept,
    defer,
    resend,
)
from mak.session.growth import header_id
from mak.session.store_view import file_of
from mak.session.types import NewSymbol

_DEFINITION_KINDS = ("function", "class")


class AdditiveSymbols:
    """Stage the new top-level helpers a reply wrote beside its targets.

    A new id can only be busy if another task is creating the same name, so a
    busy WRITE on it, or a name that exists by now, is a collision the agent
    resolves by renaming; the file's intention lock is the only real wait.
    """

    name = "additive_symbols"

    def check(self, ctx: CommitContext) -> Verdict:
        """Take each new symbol's lock and stage it after its anchor."""
        growth = ctx.growth
        if growth is None or not growth.additive:
            return ACCEPT
        extra: dict[NodeId, NewStaging] = {}
        previous: dict[str, NodeId] = {}
        for symbol in growth.additive:
            node_id = symbol.node_id
            if self._taken(ctx, node_id) or not ctx.acquire_transient(
                [(node_id, LockMode.WRITE)]
            ):
                return _collision(ctx, node_id)
            probe = SubTask(ctx.task_id, "", target_nodes=[node_id])
            if not ctx.acquire_transient(lock_requests(probe, ctx.wave.lock_policy)):
                return defer(
                    f"new symbol '{node_id}' waits for a lock on its file or class"
                )
            after = _anchor(ctx, symbol, previous)
            previous[file_of(str(node_id))] = node_id
            extra[node_id] = NewStaging(
                symbol.source, str(node_id).split("::")[1], after=after, place=True
            )
        ctx.on_commit.append(lambda: _record_additive(ctx, extra))
        return accept(extra=extra)

    @staticmethod
    def _taken(ctx: CommitContext, node_id: NodeId) -> bool:
        """Whether the id exists, was retired, or is already pending."""
        store = ctx.view.store
        return (
            ctx.view.committed(node_id) is not None
            or store.is_retired(node_id)
            or node_id in ctx.staged
            or store.get_staged(node_id) is not None
        )


def _collision(ctx: CommitContext, node_id: NodeId) -> Verdict:
    """Send a new helper whose name another task took back to be renamed."""
    file_path, _kind, name = str(node_id).split("::", 2)
    reason = f"new symbol '{node_id}' collides with one another task created"
    _log_conflict(ctx, "name_collision", file_path, reason)
    return resend(
        [reason],
        note=(
            f"You added a new top-level '{name}' in {file_path}, but another "
            "task has created a symbol with that name in the same file. "
            "Reuse the existing one if it does what you need, or give yours "
            "a different name."
        ),
        error_kind="name_collision",
    )


def _anchor(
    ctx: CommitContext, symbol: NewSymbol, previous: dict[str, NodeId]
) -> NodeId | None:
    """Return the node a new symbol goes after.

    The agent's own ``insert_after`` wins; then the helper it returned just
    before this one in the same file, so several keep their order; then the
    task's last target in the file, so a helper sits beside the code using it.
    """
    if symbol.anchored:
        return symbol.after
    file_path = file_of(str(symbol.node_id))
    if file_path in previous:
        return previous[file_path]
    store = ctx.view.store
    best: tuple[int, NodeId] | None = None
    for target in ctx.task.target_nodes:
        if file_of(str(target)) != file_path or "::" not in str(target):
            continue
        for node_id in _top_level_extent(ctx, target):
            order = store.node_order(node_id)
            # A new target has no order yet; it commits before this symbol and
            # lands at the end of its file, so it is the last thing there.
            rank = order if order is not None else sys.maxsize
            if best is None or rank >= best[0]:
                best = (rank, node_id)
    return best[1] if best else None


def _top_level_extent(ctx: CommitContext, target: NodeId) -> list[NodeId]:
    """Return the nodes a target's top-level statement spans.

    A method or class-body target sits inside a class: a helper placed right
    after it would land inside the class, so the whole class is the extent.
    """
    file_path, kind, name = (str(target).split("::", 2) + ["", ""])[:3]
    if kind not in ("method", "class_body"):
        return [target]
    owner = name.split(".", 1)[0].split("#", 1)[0]
    members = [
        n for n in ctx.view.store.list_nodes(file_path)
        if _class_owner(n) == (file_path, owner)
    ]
    return members or [target]


def _class_owner(node_id: NodeId) -> tuple[str, str] | None:
    """Return ``(file, class)`` for a class shell, method or class-body node."""
    file_path, kind, name = (str(node_id).split("::", 2) + ["", ""])[:3]
    if kind == "class":
        return file_path, name.split("#", 1)[0]
    if kind in ("method", "class_body"):
        return file_path, name.split(".", 1)[0].split("#", 1)[0]
    return None


def _record_additive(ctx: CommitContext, extra: dict[NodeId, NewStaging]) -> None:
    """Count and log the new symbols once the commit is durable."""
    for node_id, new in extra.items():
        ctx.wave.additive_symbols += 1
        ctx.log(
            EventType.ADDITIVE_SYMBOL,
            task_id=ctx.task_id,
            node_id=str(node_id),
            after=str(new.after) if new.after else None,
        )


class ImportMerge:
    """Merge a reply's ``imports`` into its files' module headers.

    Importers co-hold ``H#imports`` and each lock only the names they bind, so
    tasks adding different imports to one file commit side by side; the same
    import is merged once, and two targets for one name is a conflict. A task
    that holds the header (or the whole file) itself merges into its own
    staged source with no further locks.
    """

    name = "import_merge"

    def check(self, ctx: CommitContext) -> Verdict:
        """Merge every file's imports, or say which lock or name is in the way."""
        growth = ctx.growth
        if growth is None or not growth.imports:
            return ACCEPT
        restaged: dict[NodeId, str] = {}
        extra: dict[NodeId, NewStaging] = {}
        merged: list[tuple[str, ImportMergePlan]] = []
        for file_path, statements in growth.imports.items():
            verdict = self._merge_file(
                ctx, file_path, statements, restaged, extra, merged
            )
            if verdict is not None:
                return verdict
        if merged:
            ctx.on_commit.append(lambda: _record_imports(ctx, merged))
        return accept(restaged, extra=extra)

    def _merge_file(
        self,
        ctx: CommitContext,
        file_path: str,
        statements: list[str],
        restaged: dict[NodeId, str],
        extra: dict[NodeId, NewStaging],
        merged: list[tuple[str, ImportMergePlan]],
    ) -> Verdict | None:
        """Merge one file's imports; return a verdict only to stop the commit."""
        header = header_id(file_path)
        whole = NodeId(file_path)
        own = whole if whole in ctx.staged else header if header in ctx.staged else None
        if own is not None:
            fragment = ctx.view.store.get_staged(own)
            base = fragment.source if fragment is not None else ""
        else:
            if not ctx.acquire_transient(_import_locks(file_path, statements)):
                return defer(
                    f"imports for {file_path} wait for a task holding its module "
                    "header or one of the names they bind"
                )
            base = ctx.view.source(header) or ""
        plan = merge_imports(base, statements, reserved=_defined_names(ctx, file_path))
        if plan.conflicts:
            return _import_conflict(ctx, file_path, plan)
        if not plan.added or plan.source is None:
            return None
        merged.append((file_path, plan))
        if own is not None:
            restaged[own] = plan.source
        elif ctx.view.committed(header) is not None:
            extra[header] = NewStaging(plan.source, "module_header")
        else:
            extra[header] = NewStaging(
                plan.source, "module_header", after=None, place=True
            )
        return None


def _import_locks(
    file_path: str, statements: list[str]
) -> list[tuple[NodeId, LockMode]]:
    """Return the commit-time locks an import merge into ``file_path`` takes.

    READ on the header excludes a task rewriting it exclusively (whose commit
    would otherwise overwrite the merge); ``#imports`` is co-held by every
    importer; each bound name is exclusive, so one name gets one target.
    """
    header = header_id(file_path)
    requests: list[tuple[NodeId, LockMode]] = [
        (header, LockMode.READ),
        (imports_resource(header), LockMode.INTENT_WRITE),
        (NodeId(file_path), LockMode.INTENT_WRITE),
    ]
    names = {
        record.binding for statement in statements
        for record in statement_bindings(statement)
    }
    requests += [
        (import_name_resource(header, name), LockMode.WRITE) for name in sorted(names)
    ]
    return requests


def _defined_names(ctx: CommitContext, file_path: str) -> list[str]:
    """Top-level function and class names an import must not shadow."""
    names: list[str] = []
    for node_id in [*ctx.view.store.list_nodes(file_path), *ctx.staged]:
        parts = str(node_id).split("::")
        if len(parts) == 3 and parts[0] == file_path and parts[1] in _DEFINITION_KINDS:
            names.append(parts[2].split("#", 1)[0])
    return names


def _import_conflict(
    ctx: CommitContext, file_path: str, plan: ImportMergePlan
) -> Verdict:
    """Send back imports that would rebind a name the file already uses."""
    listed = "; ".join(plan.conflicts)
    reason = f"import conflict in {file_path}: {listed}"
    _log_conflict(ctx, "import_conflict", file_path, reason)
    return resend(
        [reason],
        note=(
            f"An import you listed for {file_path} would rebind a name the "
            f"file already uses for something else ({listed}). Use the "
            "existing binding, or import under an alias that is not taken."
        ),
        error_kind="import_conflict",
    )


def _log_conflict(ctx: CommitContext, kind: str, file_path: str, reason: str) -> None:
    """Log a growth collision; the resend itself leaves no other trace."""
    ctx.log(
        EventType.GROWTH_CONFLICT,
        task_id=ctx.task_id,
        kind=kind,
        file=file_path,
        reason=reason,
    )


def _record_imports(
    ctx: CommitContext, merged: list[tuple[str, ImportMergePlan]]
) -> None:
    """Count and log the merges once the commit is durable."""
    for file_path, plan in merged:
        ctx.wave.import_merges += 1
        ctx.log(
            EventType.IMPORT_MERGED,
            task_id=ctx.task_id,
            file=file_path,
            added=list(plan.added),
            skipped=list(plan.skipped),
        )
