"""Sort a reply's out-of-grant output into what the commit may grow by.

``map_returned_sources`` only knows ids: it splits a reply into sources for
granted nodes, ids that *look* like new helpers, and everything else. Whether a
helper is really new, whether a node outside the grant exists, and whether an
import belongs to a file the task targets are questions about the store and
the plan, so they are answered here, once per reply. The result is a
:class:`~mak.session.types.Growth` the commit checks act on.

Every id leaves this module accounted for: granted, additive, escalated, or
refused with a logged reason. Nothing an agent wrote is dropped silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from mak.agent_runner.protocol import ReturnedSources, split_import_entry
from mak.core.logging import EventType
from mak.core.types import LockMode, NodeId, NodeNeed, TaskResult
from mak.semantic.import_merge import added_imports
from mak.session.events import EventLog
from mak.session.store_view import StoreView, file_of, is_header_id
from mak.session.types import Escalation, Growth, NewSymbol
from mak.session.wave import WaveState


def header_id(file_path: str) -> NodeId:
    """Return the id of a file's module header node."""
    return NodeId(f"{file_path}::module_header::__header__")


@dataclass
class GrowthClassifier:
    """Builds a task's :class:`Growth` from one reply."""

    view: StoreView
    log: EventLog
    max_additive: int

    def classify(
        self,
        wave: WaveState,
        task_id: str,
        grant: list[NodeId],
        mapped: ReturnedSources,
        result: TaskResult,
    ) -> Growth:
        """Return what ``result`` asks for beyond ``grant``; log every refusal."""
        growth = Growth()
        fragment_files = {file_of(str(n)) for n in grant if "::" in str(n)}
        self._additive(wave, task_id, grant, mapped, result, growth)
        for node_id, _reason in mapped.dropped:
            self._out_of_grant(
                task_id, grant, fragment_files, node_id,
                result.new_sources[node_id], growth,
            )
        for need in result.needs_nodes:
            self._need(wave, task_id, grant, need, result, growth)
        for index, entry in enumerate(result.imports):
            self._import(task_id, grant, index, entry, growth)
        return growth

    def _additive(
        self,
        wave: WaveState,
        task_id: str,
        grant: list[NodeId],
        mapped: ReturnedSources,
        result: TaskResult,
        growth: Growth,
    ) -> None:
        """Accept new helpers beside a target, or say why one is not new."""
        for node_id, source in mapped.additive.items():
            if self._exists(node_id):
                seen = node_id in wave.read_sets.get(task_id, {})
                growth.escalations.append(Escalation(
                    node_id, LockMode.WRITE, source,
                    "returned a source for an existing node outside its grant"
                    if seen else
                    "another task created a symbol of this name after this "
                    "one started; if yours is a different helper, rename it",
                    coarse=self._coarse(node_id),
                ))
                continue
            if not wave.lock_policy.intention_locks:
                self._refuse(
                    task_id, grant, node_id, source,
                    "new symbols beside a target need semantic.intention_locks",
                )
                continue
            if len(growth.additive) >= self.max_additive:
                self._refuse(
                    task_id, grant, node_id, source,
                    f"more than session.max_additive_symbols "
                    f"({self.max_additive}) new symbols in one reply",
                )
                continue
            after = result.placements.get(node_id)
            anchored = after is not None and self._valid_anchor(
                node_id, after, mapped
            )
            growth.additive.append(NewSymbol(
                node_id, source, after if anchored else None, anchored
            ))

    def _out_of_grant(
        self,
        task_id: str,
        grant: list[NodeId],
        fragment_files: set[str],
        node_id: NodeId,
        source: str,
        growth: Growth,
    ) -> None:
        """Route an id the protocol could not place: imports, escalation, refusal."""
        text = str(node_id)
        if is_header_id(text) and file_of(text) in fragment_files:
            added = added_imports(self.view.source(node_id) or "", source)
            if added is not None:
                growth.imports.setdefault(file_of(text), []).extend(added)
                return
        if self._exists(node_id):
            growth.escalations.append(Escalation(
                node_id, LockMode.WRITE, source,
                "returned a source for an existing node outside its grant",
                coarse=self._coarse(node_id),
            ))
            return
        reason = "returned node id is outside the task's granted nodes"
        if "::" in text and text.split("::")[1] in ("method", "class_body"):
            reason += " (a new method needs its class granted)"
        elif file_of(text) not in {file_of(str(n)) for n in grant}:
            reason += " (a new symbol may only be added in a file the task targets)"
        self._refuse(task_id, grant, node_id, source, reason)

    def _need(
        self,
        wave: WaveState,
        task_id: str,
        grant: list[NodeId],
        need: NodeNeed,
        result: TaskResult,
        growth: Growth,
    ) -> None:
        """Record a declared need for a node the task was not given."""
        mode = LockMode.WRITE if need.mode == "write" else LockMode.READ
        if need.node_id in grant:
            return
        context = wave.task(task_id).context_nodes
        if mode is LockMode.READ and need.node_id in context:
            return
        for index, existing in enumerate(growth.escalations):
            if existing.node_id == need.node_id:
                if mode is LockMode.WRITE:
                    growth.escalations[index] = Escalation(
                        existing.node_id, mode, existing.source,
                        need.reason or existing.reason, existing.coarse,
                    )
                return
        growth.escalations.append(Escalation(
            need.node_id, mode, result.new_sources.get(need.node_id),
            need.reason or "declared in needs_nodes",
            coarse=self._coarse(need.node_id),
        ))

    def _import(
        self, task_id: str, grant: list[NodeId], index: int, entry: str, growth: Growth
    ) -> None:
        """Route one ``imports`` entry to the target file it belongs to."""
        file_path, statement = split_import_entry(entry)
        files = sorted({file_of(str(n)) for n in grant})
        if file_path is None and len(files) == 1:
            file_path = files[0]
        if file_path is None:
            reason = (
                "has no '<file>::' prefix and the task's targets span "
                f"{len(files)} files"
            )
        elif file_path not in files:
            reason = f"names {file_path}, which is not one of the task's target files"
        else:
            bucket = growth.imports.setdefault(file_path, [])
            if statement not in bucket:
                bucket.append(statement)
            return
        self.log(
            EventType.SOURCE_DROPPED,
            task_id=task_id,
            node_id=f"imports[{index}]",
            granted=[str(n) for n in grant],
            source_length=len(entry),
            reason=f"import {entry!r} {reason}",
        )

    def _exists(self, node_id: NodeId) -> bool:
        """Whether the store knows the id, live, retired, or as a whole file."""
        store = self.view.store
        if store.is_retired(node_id) or self.view.committed(node_id) is not None:
            return True
        return "::" not in str(node_id) and bool(store.list_nodes(str(node_id)))

    def _coarse(self, node_id: NodeId) -> bool:
        """Tell whether the id is a whole file or class shell (covers others' nodes)."""
        text = str(node_id)
        return "::" not in text or text.split("::")[1] == "class"

    def _valid_anchor(
        self, node_id: NodeId, after: NodeId, mapped: ReturnedSources
    ) -> bool:
        """``insert_after`` must name a node in the same file that will exist."""
        if file_of(str(after)) != file_of(str(node_id)) or after == node_id:
            return False
        return (
            self.view.store.node_order(after) is not None
            and not self.view.store.is_retired(after)
        ) or after in mapped.accepted or after in mapped.additive

    def _refuse(
        self,
        task_id: str,
        grant: list[NodeId],
        node_id: NodeId,
        source: str,
        reason: str,
    ) -> None:
        """Log a returned source the kernel will not apply, and why."""
        self.log(
            EventType.SOURCE_DROPPED,
            task_id=task_id,
            node_id=str(node_id),
            granted=[str(n) for n in grant],
            source_length=len(source),
            reason=reason,
        )
