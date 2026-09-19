r"""Cascade on the real reference graph (Wave 20, R3).

The old cascade compared the first function signature of each committed node
and then regex-matched ``\\bsymbol\\b`` across *other files*. That missed three
things by construction — same-file callers, deleted or renamed symbols (the
comparison needed a "new" signature), and every change a node id did not map
onto one-to-one (whole-file nodes) — while matching any file that merely
mentioned the word.

This module works from what the kernel actually knows:

- **what changed** is a per-file *symbol* diff of the wave (see
  :mod:`mak.semantic.symbols`): a signature changed, or a symbol vanished;
- **who depended on it** is read off the reference graph: callers of the
  symbol's defining node in the graph built *before* the wave (so a deleted
  symbol's callers are still found) and in the graph rebuilt *after* it (so a
  caller the wave itself added is found too);
- a caller whose every call to the symbol is provably compatible with the new
  signature is left alone — it was already updated, or never broken.

Methods are the one place the graph is blind: ``obj.m()`` has no resolvable
receiver. A method change therefore also reaches callers that use the owning
class (a graph edge to it) or live in its file, and call ``.m(`` textually.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass

from mak.conflict_detector.signature_check import (
    Signature,
    check_call,
    extract_calls,
    signature_for,
)
from mak.core.types import NodeId
from mak.planner.depgraph import DepGraph
from mak.semantic.symbols import SymbolChange, SymbolChangeKind, symbol_table


@dataclass(frozen=True, slots=True)
class CascadeItem:
    """One caller broken (or possibly broken) by one symbol change."""

    caller: NodeId
    change: SymbolChange
    definers: tuple[NodeId, ...]  # the nodes defining the symbol now (or before)
    renamed_to: str | None = None  # a same-bodied symbol the wave added instead


def cascade_items(
    changes: list[SymbolChange],
    pre: DepGraph | None,
    post: DepGraph,
    source_of: Callable[[NodeId], str | None],
    after_sources: Callable[[str], str | None],
) -> list[CascadeItem]:
    """Every live caller of a re-signed or deleted symbol that may be broken.

    ``source_of`` returns a node's committed source (None = not live);
    ``after_sources`` returns a file's current source, for rename hints.
    """
    items: list[CascadeItem] = []
    for change in changes:
        if change.kind is SymbolChangeKind.BODY:
            continue  # behaviour changes need D5 (differential tests), not a graph
        definers = _definers(change, pre, post)
        renamed = (
            _rename_of(change, after_sources(change.file))
            if change.kind is SymbolChangeKind.DELETED
            else None
        )
        for caller in sorted(_callers(change, definers, pre, post, source_of)):
            source = source_of(caller)
            if source is None or not _mentions(source, change.old.short):
                continue
            if change.kind is SymbolChangeKind.SIGNATURE and _all_calls_compatible(
                source, change
            ):
                continue
            items.append(CascadeItem(caller, change, tuple(sorted(definers)), renamed))
    return items


def _definers(
    change: SymbolChange, pre: DepGraph | None, post: DepGraph
) -> set[NodeId]:
    """Nodes in the changed file that define the symbol, before or after."""
    key = change.old.qualname
    found: set[NodeId] = set()
    for graph in (pre, post):
        if graph is None:
            continue
        for node in graph.definers.get(key, ()):
            if str(node).split("::", 1)[0] == change.file:
                found.add(node)
    return found


def _callers(
    change: SymbolChange,
    definers: set[NodeId],
    pre: DepGraph | None,
    post: DepGraph,
    source_of: Callable[[NodeId], str | None],
) -> set[NodeId]:
    callers: set[NodeId] = set()
    for graph in (pre, post):
        if graph is None:
            continue
        for node, refs in graph.references.items():
            if refs & definers and node not in definers:
                callers.add(node)
    if change.old.kind == "method":
        callers |= _method_callers(change, definers, pre, post, source_of)
    return callers


def _method_callers(
    change: SymbolChange,
    definers: set[NodeId],
    pre: DepGraph | None,
    post: DepGraph,
    source_of: Callable[[NodeId], str | None],
) -> set[NodeId]:
    """Nodes that use the method's class and call ``. name(`` textually."""
    owner = change.old.qualname.split(".", 1)[0]
    class_nodes: set[NodeId] = set()
    candidates: set[NodeId] = set()
    for graph in (pre, post):
        if graph is None:
            continue
        for node in graph.definers.get(owner, ()):
            if str(node).split("::", 1)[0] == change.file:
                class_nodes.add(node)
        candidates |= set(graph.references)
    pattern = re.compile(rf"\.{re.escape(change.old.short)}\s*\(")
    found: set[NodeId] = set()
    for node in candidates - definers:
        in_file = str(node).split("::", 1)[0] == change.file
        uses_class = any(
            graph is not None and graph.references.get(node, frozenset()) & class_nodes
            for graph in (pre, post)
        )
        if not (in_file or uses_class):
            continue
        source = source_of(node)
        if source is not None and pattern.search(source):
            found.add(node)
    return found


def _mentions(source: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", source) is not None


def _all_calls_compatible(source: str, change: SymbolChange) -> bool:
    """Whether every call to the symbol in ``source`` fits the new signature.

    False when there are no calls at all: a reference that is not a call (the
    function passed as a callback, a class used as a base) cannot be proven
    compatible, so it keeps its fix-up task.
    """
    signature = _new_signature(change)
    if signature is None:
        return False
    try:
        calls = [c for c in extract_calls(source) if c.func_name == change.old.short]
    except SyntaxError:
        return False
    if not calls:
        return False
    return all(check_call(signature, call) is None for call in calls)


def _new_signature(change: SymbolChange) -> Signature | None:
    """Return the new definition's parameter shape, receiver stripped."""
    if change.new is None or change.new.kind == "class":
        return None
    try:
        tree = ast.parse(change.new.source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return signature_for(node, in_class=change.new.kind == "method")
    return None


def _rename_of(change: SymbolChange, after: str | None) -> str | None:
    """Return a symbol added to the file whose body equals the deleted one's."""
    if after is None:
        return None
    old_body = _body(change.old.source)
    if old_body is None:
        return None
    for qualname, definition in symbol_table(after).items():
        if definition.kind != change.old.kind or qualname == change.old.qualname:
            continue
        if _body(definition.source) == old_body:
            return qualname
    return None


def _body(source: str) -> str | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    if len(tree.body) != 1:
        return None
    node = tree.body[0]
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return None
    return "\n".join(ast.unparse(stmt) for stmt in node.body)
