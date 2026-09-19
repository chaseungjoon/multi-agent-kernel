"""Registrar functions — shared tables of one registration call per line.

A *registrar* is the shape every shared table in MAK's benchmarks has, and the
shape plugin/route/handler tables have in real code::

    def _register_all() -> None:
        # (optional docstring)
        register("/users", users.index)
        register("/orders", orders.index)

It is detected by **structure, never by name**: one function whose body is an
optional docstring, a prelude of simple assignments (``entries = {}``,
``register = entries.__setitem__``), a flat list of ``callee(...)`` expression
statements to one callee, and an optional trailing ``return``. Placeholder
statements (``pass``, ``...``, ``raise NotImplementedError``) count as no entry,
so a stub table is a registrar with nothing in it yet.

Two kinds, because only one of them is commutative:

- **keyed** — every entry's first argument is a string literal. Appending
  ``register("/a", …)`` and ``register("/b", …)`` in either order gives the same
  table, so concurrent appenders can be merged by the kernel, and the same key
  appended twice is a *collision* (the last registration silently wins).
- **ordered** — entries without a literal key (``use(auth)``, ``use(audit)``):
  a middleware chain, a priority list. Order is meaning, so these are never
  merged automatically; they keep the plain node lock.

A registrar with no entries yet has no kind of its own (``EMPTY``): what the
appended entries look like decides, at commit, whether the append may merge. An
empty registrar must still *say* it is a table — a placeholder statement, or a
local table built empty in the prelude and returned — because a function that
is only ``return x`` is no evidence of a table at all.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from enum import StrEnum


class RegistrarKind(StrEnum):
    """Whether a registrar's entries commute."""

    KEYED = "keyed"
    ORDERED = "ordered"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class Entry:
    """One registration statement."""

    key: str | None  # the literal first argument, when there is one
    text: str  # the statement's canonical form (``ast.unparse``) — its identity
    source: str  # the statement's original source lines, dedented


@dataclass(frozen=True, slots=True)
class Registrar:
    """A parsed registrar function."""

    name: str
    callee: str | None  # None only for a registrar with no entries
    entries: tuple[Entry, ...]
    signature: str  # decorators + ``def`` line + docstring, canonical
    prelude: tuple[str, ...]
    epilogue: tuple[str, ...]

    @property
    def kind(self) -> RegistrarKind:
        """Keyed when every entry has a literal key; ordered otherwise."""
        if not self.entries:
            return RegistrarKind.EMPTY
        if all(entry.key is not None for entry in self.entries):
            return RegistrarKind.KEYED
        return RegistrarKind.ORDERED

    def keys(self) -> list[str]:
        """Return the literal keys, in entry order (unkeyed entries skipped)."""
        return [entry.key for entry in self.entries if entry.key is not None]


def parse_registrar(source: str) -> Registrar | None:
    """Return the registrar ``source`` defines, or None if it is not one."""
    parsed = _parse_function(source)
    if parsed is None:
        return None
    func, lines = parsed
    body = list(func.body)
    docstring = _docstring(body)
    if docstring is not None:
        body = body[1:]
    prelude, rest = _split_prelude(body)
    epilogue: list[ast.stmt] = []
    if rest and isinstance(rest[-1], ast.Return):
        epilogue = [rest.pop()]
    entries: list[Entry] = []
    callees: set[str] = set()
    placeholders = 0
    for stmt in rest:
        if _is_placeholder(stmt):
            placeholders += 1
            continue
        entry = _entry(stmt, lines)
        if entry is None:
            return None
        callee, parsed_entry = entry
        callees.add(callee)
        entries.append(parsed_entry)
    if len(callees) > 1:
        return None
    if not entries and not placeholders and not _returns_prelude_table(
        prelude, epilogue
    ):
        # Only a *stub* table is a registrar with nothing in it — a placeholder
        # statement, or a local table built in the prelude and returned.
        # Without this, every ``return x`` function would qualify.
        return None
    return Registrar(
        name=func.name,
        callee=next(iter(callees), None),
        entries=tuple(entries),
        signature=_signature(func, docstring),
        prelude=tuple(ast.unparse(s) for s in prelude),
        epilogue=tuple(ast.unparse(s) for s in epilogue),
    )


def classify(source: str) -> RegistrarKind | None:
    """Return the registrar kind of ``source``, or None when it is not one."""
    registrar = parse_registrar(source)
    return registrar.kind if registrar is not None else None


def appended_entries(before: str, after: str) -> list[Entry] | None:
    """Return the entries ``after`` appends to ``before``, or None.

    None means ``after`` is **not a pure append**: it removed, reordered or
    edited an existing entry, changed the prelude/epilogue/signature, stopped
    being a registrar, or changed the callee. Only a pure append is safe to
    replay on top of a newer version of the table.
    """
    old, new = parse_registrar(before), parse_registrar(after)
    if old is None or new is None:
        return None
    if (old.name, old.signature, old.prelude, old.epilogue) != (
        new.name, new.signature, new.prelude, new.epilogue
    ):
        return None
    if old.callee is not None and new.callee not in (None, old.callee):
        return None
    prefix = [entry.text for entry in old.entries]
    if [entry.text for entry in new.entries[: len(prefix)]] != prefix:
        return None
    return list(new.entries[len(prefix):])


def merge_append(current: str, entries: list[Entry]) -> str | None:
    """Append ``entries`` to the registrar in ``current``; None if impossible.

    The merge is textual so the table keeps its formatting and comments: new
    lines go after the last existing entry, or replace the placeholder of a
    stub, or follow the prelude. An entry whose statement is already present is
    skipped — two tasks registering the identical line is not a collision, it is
    one registration. The result is re-parsed, and anything that is no longer
    the same registrar with exactly the added entries is refused.
    """
    base = parse_registrar(current)
    parsed = _parse_function(current)
    if base is None or parsed is None:
        return None
    if base.callee is not None and any(
        _callee_of_text(e.text) not in (None, base.callee) for e in entries
    ):
        return None
    present = {entry.text for entry in base.entries}
    fresh = [e for e in dict.fromkeys(entries) if e.text not in present]
    if not fresh:
        return current
    func, lines = parsed
    merged = _insert(func, lines, fresh)
    check = parse_registrar(merged)
    if check is None or [e.text for e in check.entries] != [
        *(e.text for e in base.entries), *(e.text for e in fresh)
    ]:
        return None
    return merged


def duplicate_keys(source: str) -> list[str]:
    """Keys a keyed registrar in ``source`` registers more than once."""
    registrar = parse_registrar(source)
    if registrar is None:
        return []
    seen: set[str] = set()
    duplicated: list[str] = []
    for key in registrar.keys():
        if key in seen and key not in duplicated:
            duplicated.append(key)
        seen.add(key)
    return duplicated


# -- parsing helpers --------------------------------------------------------


def _parse_function(source: str) -> tuple[ast.FunctionDef, list[str]] | None:
    """Parse a source holding exactly one plain (non-async) function."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    return tree.body[0], source.splitlines(keepends=True)


def _returns_prelude_table(
    prelude: list[ast.stmt], epilogue: list[ast.stmt]
) -> bool:
    """``entries = {}; register = entries.

    __setitem__; return entries``.

    A local table: the prelude binds an empty dict/list/set literal and the
    function returns that very name.
    """
    if not epilogue or not isinstance(epilogue[0], ast.Return):
        return False
    returned = epilogue[0].value
    if not isinstance(returned, ast.Name):
        return False
    for stmt in prelude:
        value = stmt.value if isinstance(stmt, ast.Assign | ast.AnnAssign) else None
        targets = (
            stmt.targets if isinstance(stmt, ast.Assign)
            else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
        )
        if (
            any(isinstance(t, ast.Name) and t.id == returned.id for t in targets)
            and isinstance(value, ast.Dict | ast.List | ast.Set)
            and not getattr(value, "keys", None)
            and not getattr(value, "elts", None)
        ):
            return True
    return False


def _docstring(body: list[ast.stmt]) -> str | None:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[0].value.value
    return None


def _split_prelude(body: list[ast.stmt]) -> tuple[list[ast.stmt], list[ast.stmt]]:
    """Split leading simple name assignments from the rest of the body."""
    index = 0
    while index < len(body) and _is_simple_assignment(body[index]):
        index += 1
    return body[:index], body[index:]


def _is_simple_assignment(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Assign):
        return all(isinstance(t, ast.Name) for t in stmt.targets)
    return isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)


def _is_placeholder(stmt: ast.stmt) -> bool:
    """``pass``, ``. ..`` and ``raise NotImplementedError[(…)]``: an empty table."""
    if isinstance(stmt, ast.Pass):
        return True
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    ):
        return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def _entry(stmt: ast.stmt, lines: list[str]) -> tuple[str, Entry] | None:
    """Return ``(callee, entry)`` for a call statement, else None."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return None
    call = stmt.value
    if not isinstance(call.func, ast.Name | ast.Attribute):
        return None
    if isinstance(call.func, ast.Attribute) and not isinstance(
        call.func.value, ast.Name
    ):
        return None
    key: str | None = None
    if (
        call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    ):
        key = call.args[0].value
    segment = "".join(lines[stmt.lineno - 1 : stmt.end_lineno])
    entry = Entry(key=key, text=ast.unparse(stmt), source=textwrap.dedent(segment))
    return ast.unparse(call.func), entry


def _callee_of_text(text: str) -> str | None:
    try:
        stmt = ast.parse(text).body[0]
    except (SyntaxError, IndexError):
        return None
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        return ast.unparse(stmt.value.func)
    return None


def _signature(func: ast.FunctionDef, docstring: str | None) -> str:
    decorators = "".join(f"@{ast.unparse(d)}\n" for d in func.decorator_list)
    returns = f" -> {ast.unparse(func.returns)}" if func.returns else ""
    return (
        f"{decorators}def {func.name}({ast.unparse(func.args)}){returns}"
        f"|{docstring!r}"
    )


def _insert(func: ast.FunctionDef, lines: list[str], fresh: list[Entry]) -> str:
    """Insert ``fresh`` entries into the function's source lines."""
    body = list(func.body)
    indent = " " * body[0].col_offset
    new_lines = [
        textwrap.indent(entry.source, indent).rstrip("\n") + "\n" for entry in fresh
    ]
    placeholders = [s for s in body if _is_placeholder(s)]
    entries = [
        s for s in body
        if not _is_placeholder(s)
        and isinstance(s, ast.Expr)
        and isinstance(s.value, ast.Call)
    ]
    out = list(lines)
    if entries:
        at = entries[-1].end_lineno or entries[-1].lineno
        out[at:at] = new_lines
    elif placeholders:
        first = placeholders[0].lineno - 1
        last = placeholders[-1].end_lineno or placeholders[-1].lineno
        out[first:last] = new_lines
    else:
        anchor = _insertion_anchor(func)
        out[anchor:anchor] = new_lines
    return "".join(out)


def _insertion_anchor(func: ast.FunctionDef) -> int:
    """Line index after the docstring and prelude, before any ``return``."""
    body = list(func.body)
    last: ast.stmt | None = None
    for stmt in body:
        if isinstance(stmt, ast.Return):
            return stmt.lineno - 1
        last = stmt
    assert last is not None  # a parsed function always has a body
    return last.end_lineno or last.lineno
