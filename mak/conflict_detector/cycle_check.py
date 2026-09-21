"""A new import cycle between modules the wave touched (PLANS §5.

1).

PLANS §5.1 has always listed a "cycle-free dependency graph" check that nothing
implemented. Two tasks can create one without either seeing it: A adds ``from
b import helper`` to ``a.py`` while B adds ``from a import Model`` to ``b.py``.
Each file parses; each import resolves; the first import of either module
raises ``ImportError`` on a partially initialised module.

Reported: a strongly connected component of the module-level import graph that
contains a module the wave touched, that is *new* (its modules were not
already mutually cyclic before the wave), and that contains at least one
``from <module> import <name>`` edge — the kind that fails on a partially
initialised module. A cycle made only of ``import pkg.mod`` statements usually
works at runtime and is left alone (precision over recall).

Only module-level imports count: an import inside a function, or under ``if
TYPE_CHECKING:``, runs later or never, and cannot deadlock initialisation.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping

from mak.conflict_detector.cross_module_check import CrossModuleDefect
from mak.planner.depgraph import resolve_module_file

# (target file, is a from-import of a name)
_Edge = tuple[str, bool]


def import_graph(sources: Mapping[str, str]) -> dict[str, set[_Edge]]:
    """Module-level in-repo import edges for every parseable file."""
    files = frozenset(sources)
    graph: dict[str, set[_Edge]] = {}
    for path in sorted(files):
        try:
            tree = ast.parse(sources[path])
        except SyntaxError:
            continue
        graph[path] = _edges(tree, path, files)
    return graph


def _edges(tree: ast.Module, path: str, files: frozenset[str]) -> set[_Edge]:
    edges: set[_Edge] = set()
    for stmt in _eager_imports(tree.body):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                target = resolve_module_file(alias.name, files, strict=True)
                if target is not None and target != path:
                    edges.add((target, False))
            continue
        module = stmt.module or ""
        base = resolve_module_file(
            module, files, from_file=path, level=stmt.level, strict=True
        )
        for alias in stmt.names:
            dotted = f"{module}.{alias.name}" if module else alias.name
            sub = resolve_module_file(
                dotted, files, from_file=path, level=stmt.level, strict=True
            )
            if sub is not None and sub != path:
                edges.add((sub, False))
            elif base is not None and base != path:
                edges.add((base, True))
    return edges


def _eager_imports(body: list[ast.stmt]) -> list[ast.Import | ast.ImportFrom]:
    """Return imports executed at import time, excluding ``if TYPE_CHECKING`` blocks."""
    found: list[ast.Import | ast.ImportFrom] = []
    for stmt in body:
        if isinstance(stmt, ast.Import | ast.ImportFrom):
            found.append(stmt)
        elif isinstance(stmt, ast.If):
            if "TYPE_CHECKING" in ast.unparse(stmt.test):
                found.extend(_eager_imports(stmt.orelse))
            else:
                found.extend(_eager_imports([*stmt.body, *stmt.orelse]))
        elif isinstance(stmt, ast.Try):
            found.extend(_eager_imports([*stmt.body, *stmt.orelse, *stmt.finalbody]))
    return found


def strongly_connected(graph: dict[str, set[_Edge]]) -> list[frozenset[str]]:
    """Tarjan's SCCs of size > 1 (iterative, deterministic order)."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[frozenset[str]] = []
    counter = 0
    for root in sorted(graph):
        if root in index_of:
            continue
        work: list[tuple[str, list[str]]] = [
            (root, sorted(t for t, _ in graph.get(root, ())))
        ]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                nxt = pending.pop(0)
                if nxt not in index_of:
                    index_of[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, sorted(t for t, _ in graph.get(nxt, ()))))
                elif nxt in on_stack:
                    low[node] = min(low[node], index_of[nxt])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                members: set[str] = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    members.add(member)
                    if member == node:
                        break
                if len(members) > 1:
                    components.append(frozenset(members))
    return components


def check_new_cycles(
    before: Mapping[str, str],
    after: Mapping[str, str],
    scope: frozenset[str],
) -> list[CrossModuleDefect]:
    """Report import cycles in ``after`` that ``before`` did not have."""
    old = strongly_connected(import_graph(before))
    graph = import_graph(after)
    defects: list[CrossModuleDefect] = []
    for component in strongly_connected(graph):
        if not component & scope:
            continue
        if any(component <= previous for previous in old):
            continue
        name_edges = sorted(
            (src, dst)
            for src in component
            for dst, is_name in graph.get(src, ())
            if is_name and dst in component
        )
        if not name_edges:
            continue
        ring = " -> ".join(sorted(component))
        src, dst = name_edges[0]
        defects.append(CrossModuleDefect(
            kind="import_cycle",
            file=src,
            defining_file=dst,
            detail=(
                f"new import cycle among {ring}: '{src}' imports a name from "
                f"'{dst}' at module level, which fails while either module is "
                "still initialising. Move one import into the function that "
                "uses it, or move the shared code into a third module."
            ),
            subject=ring,
            site=f"cycle:{ring}",
        ))
    return defects
