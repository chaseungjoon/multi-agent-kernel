"""Import consistency across concurrent ``__header__`` edits.

When several agents edit a module's header region in the same round, their import
additions can collide. Two failure modes matter:

- **conflicting import** — two agents bind the *same name* to *different* targets
  (e.g. one writes ``from a import config`` and another ``from b import config``).
  Reconstruction would keep only one; the other agent's code silently breaks.
- **duplicate import** — two agents add the *same* import. Harmless at runtime but
  flagged so the header isn't left with redundant lines.

A binding bound to the same target by the same agent more than once is collapsed;
only cross-agent (or cross-statement) interactions are reported.

Two things the check deliberately does *not* call a conflict (Wave 11 audit):

- **One edit binding a name to several targets.** That is the conditional-import
  idiom (``try: import ujson as json`` / ``except ImportError: import json``, or
  a ``TYPE_CHECKING`` block) — legitimate within a single edit, and reporting it
  would fail a task for writing ordinary Python.
- **Two *files* binding the same name differently.** Header edits are compared
  per file, exactly as symbols are in ``name_collision_check``: ``config``
  meaning different things in two modules is normal.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from mak.conflict_detector.node_ids import file_scope_of


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One imported name: the bound local name and what it ultimately refers to."""

    binding: str  # name introduced into the namespace
    target: str  # canonical fully-qualified thing it refers to
    text: str  # human-readable source form


def extract_imports(source: str) -> list[ImportRecord]:
    """Extract every ``import`` / ``from ... import`` binding in ``source``."""
    tree = ast.parse(source)
    records: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".")[0]
                target = alias.name
                as_part = f" as {alias.asname}" if alias.asname else ""
                records.append(
                    ImportRecord(binding, target, f"import {alias.name}{as_part}")
                )
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            # Avoid a doubled separator for relative imports like ``from . import x``
            # where ``module`` already ends in a dot.
            sep = "" if module.endswith(".") else "."
            for alias in node.names:
                binding = alias.asname or alias.name
                target = f"{module}{sep}{alias.name}"
                as_part = f" as {alias.asname}" if alias.asname else ""
                records.append(
                    ImportRecord(
                        binding, target, f"from {module} import {alias.name}{as_part}"
                    )
                )
    return records


def check_import_conflicts(header_edits: dict[str, str]) -> list[str]:
    """Detect duplicate or conflicting imports across per-agent header edits.

    ``header_edits`` maps an edit key to that edit's ``__header__`` source. A key
    may be a plain agent id or a MAK node id (``file::kind::name``); when it
    carries a file component the comparison is scoped **per file**. Returns a
    list of human-readable reasons (empty if all header edits are consistent).
    """
    # (file_scope, binding) -> { target -> set of edits that bound it there }
    bindings: dict[tuple[str, str], dict[str, set[str]]] = {}
    for edit_key, source in header_edits.items():
        file_scope = file_scope_of(edit_key)
        for record in extract_imports(source):
            by_target = bindings.setdefault((file_scope, record.binding), {})
            by_target.setdefault(record.target, set()).add(edit_key)

    reasons: list[str] = []
    for (_scope, binding), targets in sorted(bindings.items()):
        if len(targets) > 1:
            editors = {key for keys in targets.values() for key in keys}
            if len(editors) < 2:
                # One edit binding a name to several targets is a conditional
                # import, not a disagreement between agents.
                continue
            described = "; ".join(
                f"'{target}' (agents: {', '.join(sorted(agents))})"
                for target, agents in sorted(targets.items())
            )
            reasons.append(
                f"conflicting import: name '{binding}' bound to multiple targets: "
                f"{described}"
            )
            continue
        # Single target — flag only if >1 distinct agent added the same import.
        (target, agents), = targets.items()
        if len(agents) > 1:
            reasons.append(
                f"duplicate import: '{binding}' (from '{target}') added by agents: "
                f"{', '.join(sorted(agents))}"
            )
    return reasons
