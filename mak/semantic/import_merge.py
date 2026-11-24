"""Merge import statements into a module header without rewriting it.

An agent that needs ``from typing import Any`` used to have one option: edit
the module header. The header is one node, so every task in a file that needs
any import serialized on it, and two tasks adding *different* imports were a
conflict. The ``imports`` channel lets the agent name the statement instead;
this module adds it to whatever the header holds at commit time.

The merge is line-based. The header's existing text, comments and formatting
are kept byte for byte; each new statement is inserted after the last
top-level import (``__future__`` imports after the docstring or the existing
future imports, where Python requires them). A name the header already binds
to the same thing is dropped; the same name bound to something else is a
conflict and is never merged.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass

from mak.conflict_detector.import_check import ImportRecord, extract_imports


@dataclass(frozen=True, slots=True)
class ImportMergePlan:
    """The outcome of merging some import statements into one source.

    ``source`` is the merged text, or None when nothing was added (or on a
    conflict). ``names`` are the bindings the merge introduces; ``conflicts``
    are human-readable reasons, and when there are any nothing is merged.
    """

    source: str | None
    added: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


def statement_bindings(statement: str) -> list[ImportRecord]:
    """Return the names one import statement binds."""
    return extract_imports(statement)


def merge_imports(
    base: str, statements: Sequence[str], *, reserved: Sequence[str] = ()
) -> ImportMergePlan:
    """Add each import in ``statements`` to ``base`` that it does not already have.

    ``reserved`` are names the module defines elsewhere (a function or class
    node in the same file); an import binding one of them would shadow it, so
    it is a conflict too.
    """
    try:
        tree = ast.parse(base)
    except SyntaxError as exc:
        reason = f"the header does not parse: {exc.msg}"
        return ImportMergePlan(None, conflicts=(reason,))
    bound = _top_level_bindings(tree)
    defined = _top_level_definitions(tree) | set(reserved)
    pending: list[str] = []
    skipped: list[str] = []
    names: list[str] = []
    conflicts: list[str] = []
    for statement in statements:
        text = statement.strip()
        fresh: list[ast.alias] = []
        node = ast.parse(text).body[0]
        aliases = node.names if isinstance(node, ast.Import | ast.ImportFrom) else []
        for alias, record in zip(aliases, statement_bindings(text), strict=True):
            problem = _clash(record, bound.get(record.binding), defined)
            if problem is not None:
                conflicts.append(problem)
            elif record.binding not in bound or _adds_submodule(record, bound):
                fresh.append(alias)
                bound[record.binding] = record
                names.append(record.binding)
        if not fresh:
            skipped.append(text)
        elif len(fresh) == len(aliases):
            pending.append(text)
        else:
            pending.append(_with_aliases(node, fresh))
    if conflicts:
        return ImportMergePlan(None, conflicts=tuple(conflicts))
    if not pending:
        return ImportMergePlan(None, skipped=tuple(skipped))
    source = base
    for text in pending:
        source = _insert(source, text)
    return ImportMergePlan(
        source, added=tuple(pending), skipped=tuple(skipped), names=tuple(names)
    )


def added_imports(old: str, new: str) -> list[str] | None:
    """Return the import statements ``new`` adds to ``old``, if that is all it does.

    None when ``new`` changes anything other than adding top-level imports —
    then it is a real header edit, not an import request.
    """
    try:
        old_tree, new_tree = ast.parse(old), ast.parse(new)
    except SyntaxError:
        return None
    old_dump = [ast.dump(stmt) for stmt in old_tree.body]
    added: list[str] = []
    remaining = list(old_dump)
    for stmt in new_tree.body:
        dumped = ast.dump(stmt)
        if dumped in remaining:
            remaining.remove(dumped)
            continue
        if not isinstance(stmt, ast.Import | ast.ImportFrom):
            return None
        added.append(ast.unparse(stmt))
    return added if not remaining and added else None


def _top_level_bindings(tree: ast.Module) -> dict[str, ImportRecord]:
    """Names bound by the module's own top-level import statements."""
    bound: dict[str, ImportRecord] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import | ast.ImportFrom):
            for record in extract_imports(ast.unparse(stmt)):
                bound.setdefault(record.binding, record)
    return bound


def _top_level_definitions(tree: ast.Module) -> set[str]:
    """Names the module's top level binds other than by import."""
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                names.update(
                    n.id for n in ast.walk(target) if isinstance(n, ast.Name)
                )
    return names


def _is_plain_module(record: ImportRecord) -> bool:
    """``import a.b`` (no alias): binds the top package, whatever the target."""
    return record.text.startswith("import ") and " as " not in record.text


def _clash(
    record: ImportRecord, existing: ImportRecord | None, defined: set[str]
) -> str | None:
    """Why ``record`` cannot be merged, or None."""
    if record.binding in defined:
        return (
            f"import conflict: '{record.text}' would shadow '{record.binding}', "
            "which the module already defines"
        )
    if existing is None or existing.target == record.target:
        return None
    if _is_plain_module(existing) and _is_plain_module(record):
        return None  # ``import os`` beside ``import os.path``: both bind ``os``
    return (
        f"import conflict: '{record.binding}' is already bound by "
        f"'{existing.text}', not '{record.text}'"
    )


def _adds_submodule(record: ImportRecord, bound: dict[str, ImportRecord]) -> bool:
    """``import os.path`` where only ``import os`` exists still loads something."""
    existing = bound[record.binding]
    return existing.target != record.target and _is_plain_module(record)


def _with_aliases(node: ast.stmt, aliases: list[ast.alias]) -> str:
    """Re-render an import statement keeping only ``aliases``."""
    if isinstance(node, ast.ImportFrom):
        narrowed = ast.ImportFrom(module=node.module, names=aliases, level=node.level)
        return ast.unparse(narrowed)
    return ast.unparse(ast.Import(names=aliases))


def _insert(source: str, statement: str) -> str:
    """Insert one import statement where it belongs in ``source``."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    future = statement.startswith("from __future__")
    index = _insertion_line(tree, lines, future=future)
    lines.insert(index, statement + "\n")
    return "".join(lines)


def _insertion_line(tree: ast.Module, lines: list[str], *, future: bool) -> int:
    """Return the 0-based line index a new import should be inserted at."""
    body = tree.body
    futures = [
        s for s in body if isinstance(s, ast.ImportFrom) and s.module == "__future__"
    ]
    imports = [s for s in body if isinstance(s, ast.Import | ast.ImportFrom)]
    if not future and imports:
        return imports[-1].end_lineno or 0
    if futures:
        return futures[-1].end_lineno or 0
    if body and _is_docstring(body[0]):
        return body[0].end_lineno or 0
    index = 0
    while index < len(lines) and lines[index].lstrip().startswith("#"):
        index += 1
    return index


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )
