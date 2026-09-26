"""Whole-repository checks, and whether a repair's postcondition holds.

Shared by the commit pipeline (a candidate edit must not introduce a defect or
leave its repair obligation unresolved), the no-op policy (an agent cannot
assert away an unresolved obligation) and post-wave analysis (which defects a
wave introduced).
"""

from __future__ import annotations

from collections.abc import Mapping

from mak.conflict_detector.attribute_check import check_module_attributes
from mak.conflict_detector.constructor_check import check_constructors
from mak.conflict_detector.cross_module_check import (
    CrossModuleDefect,
    check_cross_module_api,
)
from mak.conflict_detector.cycle_check import check_new_cycles
from mak.conflict_detector.duplicate_check import CreatedFunction, check_duplicates
from mak.conflict_detector.module_index import ModuleIndex
from mak.conflict_detector.override_check import check_overrides
from mak.core.types import RepairObligation, SubTask
from mak.semantic.symbols import symbol_table


def post_wave_checks(
    sources: Mapping[str, str], scope: frozenset[str]
) -> list[CrossModuleDefect]:
    """Every whole-repository check that judges ``scope`` against ``sources``."""
    index = ModuleIndex(sources)
    return [
        *check_cross_module_api(sources, scope),
        *check_module_attributes(index, scope),
        *check_constructors(index, scope),
        *check_overrides(index, scope),
    ]


def repair_families(
    sources: Mapping[str, str],
    task: SubTask,
    scope: frozenset[str],
    defects: list[CrossModuleDefect] | None = None,
) -> set[str]:
    """Return repair finding families still true in ``sources``.

    Most post-wave checks describe the current repository directly. Cycles
    and duplicate implementations are normally *delta* checks, so repair
    validation supplies a neutral baseline and synthetic provenance to ask
    the stronger question a postcondition needs: does this defect exist now?
    """
    current = (
        list(defects) if defects is not None else post_wave_checks(sources, scope)
    )
    obligations = task.repair_obligations
    if any(item.kind == "import_cycle" for item in obligations):
        current.extend(check_new_cycles({}, sources, scope))
    current.extend(check_duplicates(_duplicate_candidates(sources, obligations)))
    families = {defect.family_key for defect in current}
    cycle_components = {
        defect.subject for defect in current if defect.kind == "import_cycle"
    }
    families.update(
        obligation.family_key
        for obligation in obligations
        if obligation.kind == "import_cycle"
        and obligation.subject in cycle_components
    )
    return families


def _duplicate_candidates(
    sources: Mapping[str, str], obligations: tuple[RepairObligation, ...]
) -> list[CreatedFunction]:
    """Return the functions a duplicate-implementation obligation names, as is."""
    duplicate_names = {
        item.subject
        for item in obligations
        if item.kind == "duplicate_implementation" and item.subject
    }
    if not duplicate_names:
        return []
    created: list[CreatedFunction] = []
    files = {
        path
        for item in obligations
        if item.kind == "duplicate_implementation"
        for path in (item.file, item.defining_file)
    }
    for file_path in sorted(files):
        if file_path not in sources:
            continue
        for name, definition in symbol_table(sources[file_path]).items():
            if name in duplicate_names and definition.kind == "function":
                created.append(
                    CreatedFunction(file_path, name, definition.source, file_path)
                )
    return created


def required_provider_symbol_exists(
    sources: Mapping[str, str], obligation: RepairObligation
) -> bool:
    """Whether an empty-provider repair retained its requested API."""
    required = obligation.required_provider_symbol
    if required is None:
        return True
    names = ModuleIndex(sources).top_level_names(obligation.defining_file)
    return names is not None and required in names


def unresolved_obligations(
    sources: Mapping[str, str],
    task: SubTask,
    scope: frozenset[str],
    defects: list[CrossModuleDefect] | None = None,
) -> list[RepairObligation]:
    """Return the task's repair obligations ``sources`` does not yet discharge."""
    families = repair_families(sources, task, scope, defects)
    return [
        obligation
        for obligation in task.repair_obligations
        if obligation.family_key in families
        or not required_provider_symbol_exists(sources, obligation)
    ]
