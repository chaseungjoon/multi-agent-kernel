"""Enrich a task bundle with source context, record what it read, and gate it."""

from __future__ import annotations

from dataclasses import replace

from mak.config import SessionConfig
from mak.core.logging import EventType
from mak.core.types import NodeId, SubTask, TaskBundle
from mak.node_store.api_digest import public_api_digest
from mak.semantic.contracts import CONTRACT_PREFIX, render_contract, visible_contracts
from mak.semantic.read_set import build_read_set, read_set_to_json
from mak.session.concurrency import Dispatch
from mak.session.cross_file import CONTEXT_KEYS, CrossFileIndex, context_has
from mak.session.events import EventLog
from mak.session.grants import record_grant
from mak.session.store_view import StoreView
from mak.session.wave import WaveState


class DispatchEnricher:
    """Builds every dispatched bundle's context, layer by layer.

    Layer 0 is ``contract:<id>`` — every declared contract the task implements
    or builds on (its own, its providers', its context's), so a dependent is
    shown the fixed interface even before its provider's code exists. Then five
    layers, each only adding entries not present:

    1. ``write_source:<id>`` — every node the agent will modify.
    2. ``read_source:<id>`` — nodes the planner explicitly listed as context.
    3. ``read_source:<id>`` — all other nodes in the same file as any write
       target: full sight of imports, siblings, and class structure without
       relying on the planner.
    4. ``read_source:<id>`` — nodes in *other* files whose source contains any
       target symbol name (word-boundary match): cross-file callers and callees,
       bounded and quality-filtered (:class:`CrossFileIndex`).
    5. ``read_source:<id>`` / ``read_api:<id>`` — the committed output of the
       tasks this one directly ``depends_on``. Layers 1-4 all derive from code
       that already exists, so for a task whose targets are brand-new files they
       return *nothing*; this is the layer that carries what a dependency built.

    Each layer reports the context keys it added, so ``TASK_DISPATCHED`` says
    *which layer* put a node in the bundle.
    """

    def __init__(
        self,
        *,
        view: StoreView,
        config: SessionConfig,
        log: EventLog,
    ) -> None:
        self._view = view
        self._config = config
        self._log = log
        self._cross_file = CrossFileIndex(view)

    def enrich(self, wave: WaveState, bundle: TaskBundle) -> Dispatch:
        """Attach every layer of context, record what was attached, and gate it.

        The result is logged (``TASK_DISPATCHED``) and, when it is empty for a
        task that declares dependencies, refused rather than sent — see
        :class:`~mak.session.concurrency.Dispatch`.
        """
        task = wave.task(bundle.task_id)
        record_grant(wave, task)
        bundle = self._resume_bundle(wave, bundle)
        context = dict(bundle.context)
        target_files = {str(n).split("::", 1)[0] for n in bundle.target_nodes}
        layers: dict[str, list[str]] = {}
        layers["contract"] = self._add_contracts(wave, task, context)
        layers["write_targets"] = self._add_write_targets(bundle.target_nodes, context)
        layers["planner_context"] = self._add_planner_context(
            task.context_nodes, context
        )
        layers["same_file"] = self._add_same_file_siblings(
            bundle.target_nodes, context
        )
        layers["cross_file"], dropped = self._cross_file.add_references(
            bundle.target_nodes,
            target_files,
            context,
            self._config.cross_file_context_bytes,
        )
        layers["dependency_output"] = self._add_dependency_outputs(
            wave, task, context
        )
        self._record_read_set(wave, task, context, layers)
        return self._gate_dispatch(
            wave,
            task,
            replace(bundle, context=context),
            layers,
            cross_file_dropped=dropped,
        )

    @staticmethod
    def _resume_bundle(wave: WaveState, bundle: TaskBundle) -> TaskBundle:
        """Narrow a re-queued task to its open grants and attach the kernel note.

        A task the scheduler re-queues (a released parked result, a deadlock
        victim) is dispatched from its full ``SubTask``: without this it would
        redo grants it already committed, and arrive with no word of why.
        """
        progress = wave.progress.get(bundle.task_id)
        if progress is None:
            return bundle
        if progress.completed_nodes:
            bundle = replace(bundle, target_nodes=progress.remaining)
        if bundle.retry_note is None and progress.kernel_note is not None:
            bundle = replace(bundle, retry_note=progress.kernel_note)
            progress.kernel_note = None
        return bundle

    def _record_read_set(
        self,
        wave: WaveState,
        task: SubTask,
        context: dict[str, str],
        layers: dict[str, list[str]],
    ) -> None:
        """Record every node this bundle carries, at the version it carries.

        Called on the dispatching thread straight after enrichment. Commits
        happen on that same thread, so nothing can advance the store between
        the sources being read into ``context`` and the stamps taken here.
        """
        absent = [
            node_id
            for node_id in task.context_nodes
            if self._view.dependency_source(node_id) is None
        ]
        read_set = build_read_set(
            context,
            layers,
            absent,
            fetch=self._view.committed,
            expand=self._view.file_fragment_ids,
        )
        wave.read_sets[task.task_id] = read_set
        scheduler = wave.scheduler
        if scheduler is not None:
            persisted = scheduler.annotations.setdefault("read_sets", {})
            if isinstance(persisted, dict):
                persisted[task.task_id] = read_set_to_json(read_set)

    @staticmethod
    def _add_contracts(
        wave: WaveState, task: SubTask, context: dict[str, str]
    ) -> list[str]:
        """Layer 0: the declared contracts this task implements or builds on."""
        plan = wave.require_scheduler().dag.tasks
        added: list[str] = []
        for node_id, text in visible_contracts(task, plan).items():
            key = f"{CONTRACT_PREFIX}:{node_id}"
            context[key] = render_contract(node_id, text, own=node_id in task.contract)
            added.append(key)
        return added

    def _add_write_targets(
        self, target_nodes: list[NodeId], context: dict[str, str]
    ) -> list[str]:
        """Layer 1: the current source of every node the agent will modify."""
        added: list[str] = []
        for node_id in target_nodes:
            source = self._view.source(node_id)
            if source is not None:
                key = f"write_source:{node_id}"
                context[key] = source
                added.append(key)
        return added

    def _add_planner_context(
        self, context_nodes: list[NodeId], context: dict[str, str]
    ) -> list[str]:
        """Layer 2: the nodes the planner explicitly listed as context.

        A whole-file id whose file is stored as fragments has no node of its
        own, so it is assembled from them: the planner asked for the file, and
        shipping nothing for it would leave the agent blind to exactly what it
        was told to read.
        """
        added: list[str] = []
        for node_id in context_nodes:
            source = self._view.dependency_source(node_id)
            if source is not None:
                key = f"read_source:{node_id}"
                context[key] = source
                added.append(key)
        return added

    def _add_same_file_siblings(
        self, target_nodes: list[NodeId], context: dict[str, str]
    ) -> list[str]:
        """Layer 3: every other committed node in a write target's own file."""
        added: list[str] = []
        for node_id in target_nodes:
            file_path = str(node_id).split("::", 1)[0]
            for sibling_id in self._view.store.list_nodes(file_path):
                if context_has(context, sibling_id):
                    continue
                source = self._view.source(sibling_id)
                if source is not None:
                    key = f"read_source:{sibling_id}"
                    context[key] = source
                    added.append(key)
        return added

    def _add_dependency_outputs(
        self, wave: WaveState, task: SubTask, context: dict[str, str]
    ) -> list[str]:
        """Layer 5: the committed output of every task this one depends on.

        ``depends_on`` is MAK's own assertion that the earlier task's output
        matters to the later one, and by dispatch time the DAG guarantees that
        output is committed and readable. A task whose dependencies created new
        files would otherwise arrive with an empty bundle and invent their APIs.

        Direct dependencies only: the transitive closure grows without bound.
        Spending is bounded by ``session.dependency_context_bytes`` — past the
        budget an entry degrades to a public API digest rather than being dropped,
        because a test-writing task needs its dependency's *contract*, not its
        bodies, and "informed cheaply" beats "blind". Returns the keys it added.
        """
        budget = self._config.dependency_context_bytes
        if budget == 0:
            return []
        added: list[str] = []
        spent = 0
        dag = wave.require_scheduler().dag
        for dep_id in sorted(task.depends_on):
            if dep_id in dag.soft_dependencies(task.task_id) and not dag.is_complete(
                dep_id
            ):
                continue  # not written yet: its declared contract is layer 0
            for node_id in wave.task(dep_id).target_nodes:
                if context_has(context, node_id):
                    continue
                source = self._view.dependency_source(node_id)
                if not source:
                    continue
                if budget < 0 or spent + len(source) <= budget:
                    key = f"read_source:{node_id}"
                    context[key] = source
                    added.append(key)
                    spent += len(source)
                    continue
                digest = public_api_digest(source)
                if digest:
                    key = f"read_api:{node_id}"
                    context[key] = digest
                    added.append(key)
                    spent += len(digest)
        return added

    def _gate_dispatch(
        self,
        wave: WaveState,
        task: SubTask,
        bundle: TaskBundle,
        layers: dict[str, list[str]],
        *,
        cross_file_dropped: int,
    ) -> Dispatch:
        """Log what the bundle carries; refuse to dispatch a starved bundle.

        ``layers`` carries the per-layer attribution: which layer contributed
        which nodes, and how many bytes each cost, so the expensive layer of a
        bundle is identifiable from the log alone.
        """
        counts = _context_counts(bundle.context)
        total_bytes = sum(len(v) for v in bundle.context.values())
        progress = wave.progress.get(bundle.task_id)
        starved = not bundle.context and bool(task.depends_on or task.context_nodes)
        wave.dispatches += 1
        wave.context_bytes += total_bytes
        if starved:
            wave.starved_dispatches += 1
        self._log(
            EventType.TASK_DISPATCHED,
            task_id=bundle.task_id,
            attempt=(progress.attempts + 1) if progress is not None else 1,
            targets=[str(n) for n in bundle.target_nodes],
            depends_on=list(task.depends_on),
            write_sources=counts["write_source"],
            read_sources=counts["read_source"],
            read_apis=counts["read_api"],
            context_bytes=total_bytes,
            starved=starved,
            layers=_layer_report(layers, bundle.context),
            cross_file_dropped=cross_file_dropped,
        )
        if not starved:
            return Dispatch(bundle)
        return Dispatch(bundle, starved_reason=(
            "kernel defect: the bundle carried no context at all, while the task "
            f"declares {len(task.depends_on)} dependency edge(s) and "
            f"{len(task.context_nodes)} context node(s). The agent would have had "
            "to invent the APIs it was asked to build against."
        ))


def _layer_report(
    layers: dict[str, list[str]], context: dict[str, str]
) -> dict[str, dict[str, object]]:
    """Summarize each enrichment layer's contribution for the dispatch event.

    Node ids, not just counts: the layer which put a node in a bundle must be
    readable from the log *alone*.
    """
    return {
        name: {
            "count": len(keys),
            "bytes": sum(len(context[k]) for k in keys),
            "nodes": [k.split(":", 1)[1] for k in keys],
        }
        for name, keys in layers.items()
    }


def _context_counts(context: dict[str, str]) -> dict[str, int]:
    """Count a bundle's context entries per key prefix, for the dispatch event."""
    counts = dict.fromkeys(CONTEXT_KEYS, 0)
    for key in context:
        prefix = key.split(":", 1)[0]
        if prefix in counts:
            counts[prefix] += 1
    return counts
