"""Name-collision detection across symbols introduced by different agents.

If two agents each introduce a new symbol with the *same qualified name* in the
*same file* during the *same round*, only one can survive reconstruction. This
check extracts the top-level and method-level symbols each agent defines and
reports any qualified name claimed by more than one agent.

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
    """Detect symbols defined by more than one edit in the same file.

    ``symbol_edits`` maps an edit key to the source that edit introduced. A key may
    be a plain agent id, or a MAK node id of the form ``file::kind::name`` — when
    the key carries a file component, the collision is scoped **per file**: the
    same symbol name defined in two *different* files (e.g. the ``_register_all``
    of two separate registry tables, edited by one task) is legitimate and must
    not be flagged. Keys without a file component all share one scope, preserving
    the plain agent-id usage. Returns human-readable collision reasons.
    """
    # (file_scope, qualified_name) -> set of edit keys defining it
    owners: dict[tuple[str, str], set[str]] = {}
    for edit_key, source in symbol_edits.items():
        file_scope = edit_key.split("::", 1)[0] if "::" in edit_key else ""
        for symbol in extract_defined_symbols(source):
            owners.setdefault((file_scope, symbol.qualified_name), set()).add(edit_key)

    reasons: list[str] = []
    for (_scope, name), editors in sorted(owners.items()):
        if len(editors) > 1:
            reasons.append(
                f"name collision: '{name}' defined by agents: "
                f"{', '.join(sorted(editors))}"
            )
    return reasons
