"""Name-collision detection across symbols introduced by different agents.

If two agents each introduce a new symbol with the *same qualified name* in the
*same file* during the *same round*, only one can survive reconstruction (PLANS.md
§5.1). This check extracts the top-level and method-level symbols each agent
defines and reports any qualified name claimed by more than one agent.

The unit of comparison is the *agent*: a single agent legitimately defining a
symbol once is fine; the same qualified name defined by two different agents is the
collision.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SymbolDef:
    """A defined symbol, qualified within its file (e.g. ``Class.method``)."""

    qualified_name: str
    kind: str  # "function" | "class" | "method"


def extract_defined_symbols(source: str) -> list[SymbolDef]:
    """Extract top-level functions/classes and their methods from ``source``."""
    tree = ast.parse(source)
    symbols: list[SymbolDef] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbols.append(SymbolDef(node.name, "function"))
        elif isinstance(node, ast.ClassDef):
            symbols.append(SymbolDef(node.name, "class"))
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    symbols.append(
                        SymbolDef(f"{node.name}.{member.name}", "method")
                    )
    return symbols


def check_name_collisions(symbol_edits: dict[str, str]) -> list[str]:
    """Detect symbols defined by more than one agent in the same file.

    ``symbol_edits`` maps an agent id to the source that agent introduced. Returns
    a list of human-readable collision reasons (empty if there are none).
    """
    # qualified_name -> set of agents defining it
    owners: dict[str, set[str]] = {}
    for agent, source in symbol_edits.items():
        for symbol in extract_defined_symbols(source):
            owners.setdefault(symbol.qualified_name, set()).add(agent)

    reasons: list[str] = []
    for name, agents in sorted(owners.items()):
        if len(agents) > 1:
            reasons.append(
                f"name collision: '{name}' defined by agents: "
                f"{', '.join(sorted(agents))}"
            )
    return reasons
