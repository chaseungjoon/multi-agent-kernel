"""Turn post-wave findings into fix-up tasks for the next (cascade) wave."""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import replace

from mak.conflict_detector.cross_module_check import CrossModuleDefect
from mak.conflict_detector.module_index import ModuleIndex
from mak.core.types import NodeId, RepairObligation, SubTask
from mak.semantic.cascade_graph import CascadeItem
from mak.semantic.gate_types import GateFinding
from mak.semantic.symbols import SymbolChangeKind
from mak.session.store_view import StoreView, file_of
from mak.session.wave import WaveState


class FixupBuilder:
    """One fix-up task per broken file, caller, or gate finding."""

    def __init__(self, *, view: StoreView, default_agent_type: str | None) -> None:
        self._view = view
        self._agent_type = default_agent_type or ""

    def cross_module_tasks(
        self,
        wave: WaveState,
        defects: list[CrossModuleDefect],
        objective: str | None,
    ) -> list[SubTask]:
        """One fix-up task per file whose cross-module references do not resolve.

        The fix-up names the tasks whose work met in the defect and carries the
        wave's diff of both files, not just the defining modules' current source
        — the agent is told what changed on each side, and by whom.
        """
        by_file: dict[str, list[CrossModuleDefect]] = {}
        for defect in defects:
            by_file.setdefault(defect.file, []).append(defect)
        return [
            self._cross_module_task(wave, file_path, found, objective)
            for file_path, found in sorted(by_file.items())
        ]

    def _cross_module_task(
        self,
        wave: WaveState,
        file_path: str,
        found: list[CrossModuleDefect],
        objective: str | None,
    ) -> SubTask:
        """Build the fix-up for one file's cross-module defects."""
        store = self._view.store
        defining = sorted({d.defining_file for d in found} - {file_path})
        provider_targets = sorted(
            {d.defining_file for d in found if self._repair_needs_provider(d)}
        )
        target_files = {file_path, *provider_targets}
        targets = list(store.list_nodes(file_path))
        targets.extend(NodeId(path) for path in provider_targets)
        return SubTask(
            task_id=fixup_task_id("api_fix", file_path),
            description=(
                self._cross_module_description(file_path, found, defining)
                + _provider_note(provider_targets)
                + (
                    f" Preserve the original user objective: {objective}"
                    if objective
                    else ""
                )
                + self._pair_context(wave, file_path, defining)
            ),
            target_nodes=list(dict.fromkeys(targets)),
            context_nodes=[
                node
                for path in defining
                if path not in target_files
                for node in store.list_nodes(path)
            ],
            depends_on=[],
            agent_type=self._agent_type,
            repair_obligations=tuple(
                _obligation(defect, provider_targets) for defect in found
            ),
        )

    @staticmethod
    def _cross_module_description(
        file_path: str, found: list[CrossModuleDefect], defining: list[str]
    ) -> str:
        listed = "; ".join(d.detail for d in found)
        return (
            f"Fix `{file_path}` so its use of "
            f"{', '.join(f'`{d}`' for d in defining) or 'its own code'} "
            f"matches what those modules actually define: {listed}. Use "
            "the real names and signatures — do not add fallbacks or "
            "try/except around the imports."
        )

    def cascade_tasks(self, items: list[CascadeItem]) -> list[SubTask]:
        """One fix-up task per broken caller, naming every change it must absorb."""
        by_caller: dict[NodeId, list[CascadeItem]] = {}
        for item in items:
            by_caller.setdefault(item.caller, []).append(item)
        tasks: list[SubTask] = []
        for caller, found in sorted(by_caller.items()):
            symbols = sorted({i.change.old.qualname for i in found})
            context = sorted({
                d for i in found for d in i.definers
                if self._view.source(d) is not None and d != caller
            })
            for item in found:
                if not any(file_of(str(c)) == item.change.file for c in context):
                    context.extend(self._view.store.list_nodes(item.change.file))
            tasks.append(SubTask(
                task_id=fixup_task_id("cascade", f"{'_'.join(symbols)}_{caller}"),
                description=(
                    f"Update `{caller}` for changes this wave made to code it "
                    "uses:\n"
                    + "\n".join(describe_cascade(i) for i in found)
                    + "\nAdjust every use in this node to match — do not restore "
                    "the old definitions or add compatibility shims."
                ),
                target_nodes=[caller],
                context_nodes=list(dict.fromkeys(n for n in context if n != caller)),
                depends_on=[],
                agent_type=self._agent_type,
            ))
        return tasks

    def gate_tasks(self, findings: list[GateFinding]) -> list[SubTask]:
        """One fix-up task per gate and file, naming the task(s) it traces to."""
        grouped: dict[tuple[str, str], list[GateFinding]] = {}
        for finding in findings:
            if finding.targets:
                grouped.setdefault((finding.gate, finding.file), []).append(finding)
        tasks: list[SubTask] = []
        for (gate, file_path), found in sorted(grouped.items()):
            culprits = sorted({t for f in found for t in f.tasks})
            tasks.append(SubTask(
                task_id=fixup_task_id(f"{gate}_fix", file_path),
                description=(
                    f"The {gate.replace('_', ' ')} gate found a problem this "
                    f"wave introduced (task(s) {', '.join(culprits) or 'unknown'}): "
                    + "; ".join(f.detail for f in found)
                    + ". Fix the code so the combined result is correct — do not "
                    "weaken or delete the check that caught it."
                ),
                target_nodes=list(dict.fromkeys(n for f in found for n in f.targets)),
                context_nodes=list(dict.fromkeys(n for f in found for n in f.context)),
                depends_on=[],
                agent_type=self._agent_type,
            ))
        return tasks

    def _repair_needs_provider(self, defect: CrossModuleDefect) -> bool:
        """Whether an unresolved import points at an empty static provider."""
        if defect.kind != "unresolved_import":
            return False
        source = self._view.file_source_or_empty(defect.defining_file)
        names = ModuleIndex({defect.defining_file: source}).top_level_names(
            defect.defining_file
        )
        return names == frozenset()

    def _pair_context(
        self, wave: WaveState, file_path: str, defining: list[str]
    ) -> str:
        """Name the tasks behind a defect and show what each side changed."""
        parts: list[str] = []
        for path in [file_path, *defining]:
            writers = wave.file_writers.get(path)
            if not writers:
                continue
            diff = bounded_diff(
                wave.file_before.get(path) or "",
                self._view.file_source_or_empty(path),
                path,
            )
            parts.append(
                f"\n\n`{path}` was changed this wave by task(s) "
                f"{', '.join(writers)}:\n{diff}"
            )
        return "".join(parts)


def _provider_note(provider_targets: list[str]) -> str:
    """Tell the agent an empty provider is in scope and must gain the API."""
    if not provider_targets:
        return ""
    providers = ", ".join(f"`{path}`" for path in provider_targets)
    return (
        f" {providers} exports no statically visible symbol that can "
        "satisfy "
        "the new use, so a caller-only rename cannot solve this. "
        "The provider is writable: implement the required API or "
        "reconcile both sides; do not substitute another guessed "
        "name or remove requested behavior."
    )


def _obligation(
    defect: CrossModuleDefect, provider_targets: list[str]
) -> RepairObligation:
    """Return the kernel-owned postcondition a fix-up for ``defect`` must meet."""
    return RepairObligation(
        kind=defect.kind,
        file=defect.file,
        defining_file=defect.defining_file,
        detail=defect.detail,
        exact_key=defect.exact_key,
        family_key=defect.family_key,
        subject=defect.subject,
        site=defect.site,
        required_provider_symbol=(
            defect.subject if defect.defining_file in provider_targets else None
        ),
    )


def fixup_task_id(prefix: str, subject: str) -> str:
    """Build a collision-free task id for a generated fix-up task.

    Sanitizing to ``[a-zA-Z0-9_]`` is lossy: ``a/b.py`` and ``a-b.py`` both
    become ``a_b_py``, and ``DAG`` rejects a duplicate task id outright — two
    unrelated files whose names happen to sanitize alike would take down the
    whole cascade wave. The digest is of the *original* subject, so ids that
    differ before sanitizing still differ after it.
    """
    slug = re.sub(r"[^a-zA-Z0-9]", "_", f"{prefix}_{subject}")
    digest = hashlib.blake2s(subject.encode("utf-8"), digest_size=4).hexdigest()
    return f"{slug}_{digest}"


def bounded_diff(before: str, after: str, path: str, limit: int = 3000) -> str:
    """Return a unified diff of one file's wave, truncated to ``limit`` chars."""
    text = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path} (before this wave)",
            tofile=f"{path} (now)",
        )
    )
    if len(text) > limit:
        return text[:limit] + "\n… (diff truncated)"
    return text or "(no textual change)"


def describe_cascade(item: CascadeItem) -> str:
    """Return one line (plus a bounded diff) describing a change to absorb."""
    change = item.change
    where = f"`{change.old.qualname}` in `{change.file}`"
    if change.kind is SymbolChangeKind.DELETED:
        hint = (
            f" — it appears to be renamed to `{item.renamed_to}`"
            if item.renamed_to else ""
        )
        return f"- {where} was deleted this wave{hint}."
    assert change.new is not None
    diff = bounded_diff(
        change.old.source, change.new.source, change.old.qualname, 1500
    )
    return (
        f"- {where}: signature changed from `{change.old.signature}` to "
        f"`{change.new.signature}`.\n{diff}"
    )


def merge_fixups(fixes: list[SubTask], extra: list[SubTask]) -> list[SubTask]:
    """Fold a fix-up into an earlier one that already targets its node."""
    merged = list(fixes)
    for task in extra:
        home = next(
            (
                i for i, fix in enumerate(merged)
                if set(task.target_nodes) <= set(fix.target_nodes)
            ),
            None,
        )
        if home is None:
            merged.append(task)
            continue
        fix = merged[home]
        merged[home] = replace(
            fix,
            description=f"{fix.description}\n\nAlso: {task.description}",
            context_nodes=list(
                dict.fromkeys([*fix.context_nodes, *task.context_nodes])
            ),
            repair_obligations=tuple(
                dict.fromkeys([*fix.repair_obligations, *task.repair_obligations])
            ),
        )
    return merged
