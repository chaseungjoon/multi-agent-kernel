"""The one record every plan check reports: :class:`PlanFinding`.

Kept apart from :mod:`mak.planner.validation` so the checks that validation
runs (caller completion among them) can report findings without importing the
module that runs them. ``mak.planner.validation`` re-exports it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlanFinding:
    """One deterministic observation about a plan, for review and logging.

    ``kind`` is one of: ``missing_dep``, ``spurious_dep``, ``unknown_node``,
    ``corrected_node``, ``context_dropped``, ``relaxed_dep``,
    ``declared_api_dep``, ``shared_structure``, ``ordered_table``,
    ``registry_key_collision``, ``unseen_target``, ``missing_caller``,
    ``undeclared_api_callers``, ``caller_tasks_capped`` and
    ``caller_task_refused``.
    """

    kind: str
    task_id: str
    message: str
    suggestions: tuple[str, ...] = ()
