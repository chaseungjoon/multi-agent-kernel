"""After a wave: what it left broken between tasks, as fix-up tasks.

Every check MAK runs during a wave is scoped to one task's edit, so two tasks
can each finish clean and still leave the codebase broken between them. This
module looks at the wave as a whole, against the store as it now stands.
"""

from __future__ import annotations

import hashlib

from mak.config import SemanticConfig
from mak.conflict_detector.cross_module_check import CrossModuleDefect
from mak.conflict_detector.cycle_check import check_new_cycles
from mak.conflict_detector.duplicate_check import CreatedFunction, check_duplicates
from mak.core.exceptions import SemanticGateError
from mak.core.logging import EventType
from mak.core.types import NodeFragment, NodeId, SubTask
from mak.node_store.reconstruction import assemble_fragments
from mak.planner.depgraph import dep_graph_from_store
from mak.semantic.cascade_graph import CascadeItem, cascade_items
from mak.semantic.gate_types import GateFinding, WaveView
from mak.semantic.gates import GateSuite
from mak.semantic.sources import StoreSources
from mak.semantic.symbols import SymbolChangeKind, diff_symbols, symbol_table
from mak.session.events import EventLog
from mak.session.fixups import FixupBuilder, describe_cascade, merge_fixups
from mak.session.repair import post_wave_checks
from mak.session.store_view import StoreView, file_of
from mak.session.wave import WaveState
from mak.session.workspace import Workspace


class PostWaveAnalyzer:
    """Cross-module defects, the signature cascade, and the optional gates.

    Each finding is logged once per store generation (``CONFLICT_DETECTED`` or
    ``GATE_FINDING``), so it is on the record even if the operator declines the
    fix-up wave; results are cached per generation on the wave, because the
    cascade loop asks twice for one state.
    """

    def __init__(
        self,
        *,
        view: StoreView,
        workspace: Workspace,
        gates: GateSuite,
        config: SemanticConfig,
        fixups: FixupBuilder,
        log: EventLog,
    ) -> None:
        self._view = view
        self._workspace = workspace
        self._gates = gates
        self._config = config
        self.fixups = fixups
        self._log = log

    def take_gate_baseline(self) -> None:
        """Record the type checker's pre-existing diagnostics, when it is on."""
        try:
            self._gates.take_baseline(self._workspace.work_dir)
        except SemanticGateError as exc:
            self._log(EventType.GATE_FINDING, gate="type_check", error=str(exc))

    def detect_cascade_tasks(
        self, wave: WaveState, objective: str | None
    ) -> list[SubTask]:
        """Return fix-up tasks for everything this wave left broken between tasks.

        Three sources, one review flow:

        - **cross-module defects** (:meth:`detect_cross_module_defects`) — code
          the wave wrote that contradicts the code it uses;
        - **cascade** — callers of every symbol whose signature changed or which
          was deleted/renamed this wave, found on the real reference graph
          (before and after the wave), same-file callers included, minus callers
          whose calls already fit the new signature;
        - **gates** — whatever the optional type-check, impact-test and
          import-smoke gates found, when a project turns them on.

        A caller named by more than one is given one task, not several. Returns
        an empty list when nothing is broken. Idempotent for an unchanged store.
        """
        fixes = self.fixups.cross_module_tasks(
            wave, self.detect_cross_module_defects(wave), objective
        )
        cascade = self.fixups.cascade_tasks(self._cascade_items(wave))
        gated = self.fixups.gate_tasks(self._gate_findings(wave))
        return merge_fixups(merge_fixups(fixes, cascade), gated)

    def detect_cross_module_defects(self, wave: WaveState) -> list[CrossModuleDefect]:
        """Report where the files this wave wrote contradict the rest of the code.

        A module that imports a name its target never defines, calls a sibling's
        function with the wrong arity, reads ``mod.name`` from a module that no
        longer binds it, constructs a class its new fields reject, overrides a
        base method it can no longer honour, closes an import cycle, or
        duplicates a function another task just wrote.

        Scope is every file with a commit this wave, and only defects the wave
        **introduced** are reported: the same checks run over the pre-wave state,
        and anything they also find there is pre-existing debt, not this wave's
        fix-up work.
        """
        scope = frozenset(wave.file_before) | frozenset(
            file_of(str(n)) for n in wave.committed
        )
        if not scope:
            return []
        generation = self._view.store.generation
        if wave.defects_at is not None and wave.defects_at[0] == generation:
            return list(wave.defects_at[1])
        after = StoreSources(self._view.store)
        before = after.with_overrides({
            f: wave.file_before.get(f) for f in scope if f in wave.file_before
        })
        baseline = {d.detail for d in post_wave_checks(before, scope)}
        defects = [
            d for d in post_wave_checks(after, scope) if d.detail not in baseline
        ]
        defects.extend(check_new_cycles(before, after, scope))
        defects.extend(
            check_duplicates(_created_functions(wave, before, after, scope))
        )
        for defect in defects:
            self._log(
                EventType.CONFLICT_DETECTED,
                kind=defect.kind,
                file=defect.file,
                defining_file=defect.defining_file,
                reasons=[defect.detail],
            )
        wave.defects_at = (generation, list(defects))
        return defects

    def fingerprint(self, tasks: list[SubTask]) -> str:
        """Digest the broken state and repair scope shown for cascade review.

        Store generation is deliberately absent: A -> B -> A changes generation
        twice but returns to the same source. The digest covers implicated file
        contents, target scope and kernel-owned defect families, so an identical
        state and a two-state oscillation are both recognizable.
        """
        files = sorted(
            {
                file_of(str(node))
                for task in tasks
                for node in (*task.target_nodes, *task.context_nodes)
            }
        )
        families = sorted(
            {
                obligation.family_key
                for task in tasks
                for obligation in task.repair_obligations
            }
        )
        scope = sorted(
            (task.task_id, *(str(node) for node in task.target_nodes)) for task in tasks
        )
        digest = hashlib.blake2s(digest_size=16)
        for file_path in files:
            digest.update(file_path.encode())
            digest.update(b"\0")
            digest.update(self._view.file_source_or_empty(file_path).encode())
            digest.update(b"\0")
        for family in families:
            digest.update(family.encode())
            digest.update(b"\0")
        for item in scope:
            digest.update("\x1f".join(item).encode())
            digest.update(b"\0")
        return digest.hexdigest()

    def _cascade_items(self, wave: WaveState) -> list[CascadeItem]:
        """Callers of symbols this wave re-signed or deleted, on the real graph."""
        generation = self._view.store.generation
        if wave.cascade_at is not None and wave.cascade_at[0] == generation:
            return list(wave.cascade_at[1])
        items = self._find_cascade_items(wave)
        for item in items:
            self._log(
                EventType.CONFLICT_DETECTED,
                kind="cascade",
                file=file_of(str(item.caller)),
                defining_file=item.change.file,
                reasons=[describe_cascade(item).splitlines()[0]],
            )
        wave.cascade_at = (generation, list(items))
        return items

    def _find_cascade_items(self, wave: WaveState) -> list[CascadeItem]:
        """Diff this wave's symbols and walk the reference graph for callers."""
        scope = sorted(wave.file_before)
        if not scope:
            return []
        changes = [
            change
            for file_path in scope
            for change in diff_symbols(
                file_path,
                wave.file_before.get(file_path) or "",
                self._view.file_source_or_empty(file_path),
            )
        ]
        if not any(c.kind is not SymbolChangeKind.BODY for c in changes):
            return []
        return cascade_items(
            changes,
            wave.graph,
            dep_graph_from_store(self._view.store),
            source_of=self._view.source,
            after_sources=lambda f: self._view.file_source_or_empty(f) or None,
        )

    def _gate_findings(self, wave: WaveState) -> list[GateFinding]:
        """Run the enabled gates over this wave, once per store generation."""
        if not self._gates.enabled or not wave.file_before:
            return []
        generation = self._view.store.generation
        if wave.gates_at is not None and wave.gates_at[0] == generation:
            return list(wave.gates_at[1])
        findings = self._gates.run(
            self._wave_view(wave),
            log=lambda **p: self._log(EventType.GATE_FINDING, **p),
        )
        for finding in findings:
            self._log(
                EventType.GATE_FINDING,
                gate=finding.gate,
                file=finding.file,
                tasks=list(finding.tasks),
                detail=finding.detail,
            )
        wave.gates_at = (generation, list(findings))
        return findings

    def _wave_view(self, wave: WaveState) -> WaveView:
        """Return what the gates may see of the wave that just ran."""
        tasks = tuple(dict.fromkeys(task for task, *_ in wave.commit_log))
        return WaveView(
            work_dir=self._workspace.work_dir,
            before=dict(wave.file_before),
            current=lambda f: self._view.file_source_or_empty(f) or None,
            subset=lambda chosen: subset_files(wave, chosen),
            writers={f: list(w) for f, w in wave.file_writers.items()},
            tasks=tasks,
            task_nodes=lambda t: list(dict.fromkeys(
                node for task, node, *_ in wave.commit_log if task == t
            )),
            file_nodes=lambda f: self._view.store.list_nodes(f),
            timeout_s=self._config.gate_timeout_s,
            max_overlays=self._config.impact_max_overlays,
        )


def subset_files(wave: WaveState, tasks: frozenset[str]) -> dict[str, str | None]:
    """Every touched file as the pre-wave state plus only ``tasks``' commits.

    Rebuilt from fragments, not files: each touched file's pre-wave
    fragments, with the chosen tasks' committed nodes swapped in (in commit
    order) and a whole-file commit superseding the fragments before it.
    """
    files: dict[str, str | None] = {}
    for file_path in wave.file_before:
        nodes: dict[NodeId, tuple[int | None, str]] = {
            node: (order, source)
            for node, order, source in wave.fragments_before.get(file_path, [])
        }
        for task, node, order, source in wave.commit_log:
            if task not in tasks or file_of(str(node)) != file_path:
                continue
            if "::" not in str(node):
                nodes = {}
            nodes[node] = (order, source)
        ordered = sorted(
            nodes.items(),
            key=lambda item: (item[1][0] is None, item[1][0] or 0, str(item[0])),
        )
        files[file_path] = (
            assemble_fragments(
                [NodeFragment(n, "fragment", src, 1) for n, (_, src) in ordered]
            )
            if ordered
            else None
        )
    return files


def _created_functions(
    wave: WaveState, before: StoreSources, after: StoreSources, scope: frozenset[str]
) -> list[CreatedFunction]:
    """Top-level functions this wave created, with the task that wrote each."""
    created: list[CreatedFunction] = []
    for file_path in sorted(scope):
        if file_path not in after:
            continue
        old = symbol_table(before[file_path]) if file_path in before else {}
        for name, definition in symbol_table(after[file_path]).items():
            if definition.kind != "function" or name in old:
                continue
            writer = _writer_of(wave, file_path, name)
            if writer is not None:
                created.append(
                    CreatedFunction(file_path, name, definition.source, writer)
                )
    return created


def _writer_of(wave: WaveState, file_path: str, name: str) -> str | None:
    """Return the task that committed the node defining ``name``."""
    for node_id in (
        NodeId(f"{file_path}::function::{name}"),
        NodeId(file_path),
    ):
        if node_id in wave.node_writer:
            return wave.node_writer[node_id]
    writers = wave.file_writers.get(file_path)
    return writers[-1] if writers else None
