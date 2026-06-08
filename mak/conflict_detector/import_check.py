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
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


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

    ``header_edits`` maps an agent id to that agent's ``__header__`` source. Returns
    a list of human-readable reasons (empty if all header edits are consistent).
    """
    # binding -> { target -> sorted set of agents that bound it to that target }
    bindings: dict[str, dict[str, set[str]]] = {}
    for agent, source in header_edits.items():
        for record in extract_imports(source):
            by_target = bindings.setdefault(record.binding, {})
            by_target.setdefault(record.target, set()).add(agent)

    reasons: list[str] = []
    for binding, targets in bindings.items():
        if len(targets) > 1:
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
