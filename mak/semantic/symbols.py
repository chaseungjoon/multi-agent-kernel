"""Symbol tables and symbol-level diffs of Python sources.

Post-wave analysis used to work on *node ids*: "which nodes were committed,
and did their first function's signature change?". That misses every change
that does not map one-to-one onto a node — a whole-file node that dropped a
function, a fragment rewritten to define a differently named function, a
method removed from a class stored as one node. Working on **symbols** (the
qualified names a file defines, ``f`` / ``C`` / ``C.m``) sees all of them the
same way, however the file happens to be stored.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from enum import StrEnum

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True, slots=True)
class SymbolDef:
    """One definition: its qualified name, kind, signature and source."""

    qualname: str  # ``f``, ``C``, ``C.m``
    kind: str  # ``function`` | ``class`` | ``method``
    signature: str  # ``def f(a, b) -> R`` / ``class C(Base)``
    source: str  # the definition's source, dedented

    @property
    def short(self) -> str:
        """The unqualified name (``m`` for ``C. m``)."""
        return self.qualname.rsplit(".", 1)[-1]


class SymbolChangeKind(StrEnum):
    """How a symbol differs between two versions of a file."""

    SIGNATURE = "signature"
    DELETED = "deleted"
    BODY = "body"


@dataclass(frozen=True, slots=True)
class SymbolChange:
    """One symbol that changed in a file this wave."""

    file: str
    kind: SymbolChangeKind
    old: SymbolDef
    new: SymbolDef | None  # None when deleted


def symbol_table(source: str) -> dict[str, SymbolDef]:
    """Every top-level function and class, and each class's methods.

    Returns ``{}`` when the source does not parse. Later definitions of the same
    name win, as they do at runtime.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    lines = source.splitlines(keepends=True)
    table: dict[str, SymbolDef] = {}
    for stmt in tree.body:
        if isinstance(stmt, _FUNC):
            table[stmt.name] = _def(stmt, stmt.name, "function", lines)
        elif isinstance(stmt, ast.ClassDef):
            table[stmt.name] = _def(stmt, stmt.name, "class", lines)
            for member in stmt.body:
                if isinstance(member, _FUNC):
                    qualname = f"{stmt.name}.{member.name}"
                    table[qualname] = _def(member, qualname, "method", lines)
    return table


def diff_symbols(file: str, before: str, after: str) -> list[SymbolChange]:
    """Symbols of ``before`` that were deleted, re-signed, or re-bodied.

    New symbols are not changes: nothing existed to depend on them. A rename is
    a deletion of the old name (and an addition, which is not reported).
    """
    old, new = symbol_table(before), symbol_table(after)
    changes: list[SymbolChange] = []
    for qualname, definition in old.items():
        current = new.get(qualname)
        if current is None:
            changes.append(
                SymbolChange(file, SymbolChangeKind.DELETED, definition, None)
            )
        elif current.signature != definition.signature:
            changes.append(
                SymbolChange(file, SymbolChangeKind.SIGNATURE, definition, current)
            )
        elif current.source != definition.source:
            changes.append(
                SymbolChange(file, SymbolChangeKind.BODY, definition, current)
            )
    return changes


def symbol_source(source: str, qualname: str) -> str | None:
    """Return the dedented source of ``qualname`` in ``source``, if defined."""
    definition = symbol_table(source).get(qualname)
    return definition.source if definition is not None else None


def bound_names(source: str) -> set[str]:
    """Names ``source`` binds at its top level and inside its classes.

    Functions, classes, methods, assignment targets and import bindings — the
    names another piece of code could refer to. Lenient about class-shell
    fragments that do not parse on their own.
    """
    tree = _parse_lenient(source)
    if tree is None:
        return set()
    names: set[str] = set()
    for stmt in tree.body:
        names |= _names_bound_by(stmt)
        if isinstance(stmt, ast.ClassDef):
            for member in stmt.body:
                names |= _names_bound_by(member)
    return names


def _names_bound_by(stmt: ast.stmt) -> set[str]:
    if isinstance(stmt, (*_FUNC, ast.ClassDef)):
        return {stmt.name}
    if isinstance(stmt, ast.Assign):
        return {t.id for t in stmt.targets if isinstance(t, ast.Name)}
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return {stmt.target.id}
    if isinstance(stmt, ast.Import | ast.ImportFrom):
        return {
            alias.asname or alias.name.split(".")[0]
            for alias in stmt.names
            if alias.name != "*"
        }
    return set()


def _parse_lenient(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except SyntaxError:
        pass
    try:
        return ast.parse(source.rstrip() + "\n    pass\n")
    except SyntaxError:
        return None


def _def(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    qualname: str,
    kind: str,
    lines: list[str],
) -> SymbolDef:
    start = min(
        [d.lineno for d in node.decorator_list], default=node.lineno
    )
    end = node.end_lineno or node.lineno
    source = textwrap.dedent("".join(lines[start - 1 : end]))
    return SymbolDef(qualname, kind, signature_of(node), source)


def signature_of(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """Render a definition's signature: parameters, return, bases, decorators."""
    decorators = "".join(f"@{ast.unparse(d)} " for d in node.decorator_list)
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(
            [*(ast.unparse(b) for b in node.bases),
             *(ast.unparse(k) for k in node.keywords)]
        )
        return f"{decorators}class {node.name}({bases})"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{decorators}{prefix} {node.name}({ast.unparse(node.args)}){returns}"
