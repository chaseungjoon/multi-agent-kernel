"""A read-only index of a repository's modules, shared by the post-wave checks.

Every cross-file check asks the same questions — which in-repo file does this
import name, what does that module bind, which class does this expression
refer to — and each answer is only worth anything if all the checks agree on
it. So resolution lives here once, built on the same *strict*
:func:`~mak.planner.depgraph.resolve_module_file` the cross-module check uses:
an import resolves to a repo file only when its whole dotted tail matches, so a
third-party ``from x.y import z`` never lands on an unrelated local ``y.py``.

Sources are read lazily through the mapping handed in, so a caller can pass a
mapping that assembles files on demand and pay only for what a check touches.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass

from mak.planner.depgraph import resolve_module_file


@dataclass(frozen=True, slots=True)
class SymbolRef:
    """A name bound by ``from <module> import <name>``, resolved to its file."""

    file: str
    name: str


class ModuleIndex:
    """Parsed modules and their import bindings, memoized per file."""

    def __init__(self, sources: Mapping[str, str]) -> None:
        self._sources = sources
        self.files: frozenset[str] = frozenset(sources)
        self._trees: dict[str, ast.Module | None] = {}

    def source(self, path: str) -> str | None:
        """Return the file's source, or None when the index does not hold it."""
        return self._sources.get(path) if path in self.files else None

    def tree(self, path: str) -> ast.Module | None:
        """Return the parsed module, or None when absent or unparseable."""
        if path not in self._trees:
            source = self.source(path)
            try:
                self._trees[path] = ast.parse(source) if source is not None else None
            except SyntaxError:
                self._trees[path] = None
        return self._trees[path]

    # -- what a module binds ---------------------------------------------

    def top_level_names(self, path: str) -> frozenset[str] | None:
        """Every name the module binds at top level; None when unknowable.

        Unknowable covers an unparseable module and one that binds names
        dynamically — a module-level ``__getattr__``, a star import, or a write
        to ``globals()`` — because none of those can be proven *not* to define
        a given name.
        """
        tree = self.tree(path)
        if tree is None or _is_dynamic(tree):
            return None
        names: set[str] = set()
        for stmt in _module_level(tree):
            names |= _bound_by(stmt)
        return frozenset(names)

    def is_submodule(self, package_file: str, name: str) -> bool:
        """Whether ``name`` is a module inside the package ``package_file`` opens."""
        if not package_file.endswith("__init__.py"):
            return False
        directory = package_file[: -len("__init__.py")]
        return (
            f"{directory}{name}.py" in self.files
            or f"{directory}{name}/__init__.py" in self.files
        )

    def classes(self, path: str) -> dict[str, ast.ClassDef]:
        """Top-level classes defined in the module, by name (later wins)."""
        tree = self.tree(path)
        if tree is None:
            return {}
        return {s.name: s for s in tree.body if isinstance(s, ast.ClassDef)}

    # -- what a module imports -------------------------------------------

    def module_bindings(self, path: str) -> dict[str, str]:
        """Local names bound to an in-repo *module*, to that module's file.

        ``import a.b as m`` → ``m``; ``import a`` → ``a``; ``from pkg import
        mod`` where ``pkg/mod.py`` exists → ``mod``. ``import a.b`` without an
        alias binds ``a``, not the submodule, and is resolved as ``a``.
        """
        tree = self.tree(path)
        if tree is None:
            return {}
        bindings: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.name if alias.asname else alias.name.split(".")[0]
                    resolved = resolve_module_file(target, self.files, strict=True)
                    if resolved is not None and resolved != path:
                        bindings[alias.asname or target] = resolved
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    dotted = (
                        f"{node.module}.{alias.name}" if node.module
                        else alias.name
                    )
                    resolved = resolve_module_file(
                        dotted, self.files, from_file=path, level=node.level,
                        strict=True,
                    )
                    if resolved is not None and resolved != path:
                        bindings[alias.asname or alias.name] = resolved
        return bindings

    def symbol_bindings(self, path: str) -> dict[str, SymbolRef]:
        """Local names bound by ``from <in-repo module> import <symbol>``."""
        tree = self.tree(path)
        if tree is None:
            return {}
        bindings: dict[str, SymbolRef] = {}
        modules = self.module_bindings(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            defining = resolve_module_file(
                node.module or "", self.files, from_file=path, level=node.level,
                strict=True,
            )
            if defining is None or defining == path:
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == "*" or local in modules:
                    continue
                bindings[local] = SymbolRef(defining, alias.name)
        return bindings

    def resolve_class(
        self, path: str, expr: ast.expr
    ) -> tuple[str, ast.ClassDef] | None:
        """Resolve a class reference written in ``path`` to its definition.

        ``C`` (defined in the file, or from-imported) and ``mod.C`` (``mod`` an
        in-repo module) are resolvable; anything else — a call, a subscript, a
        name rebound locally — is not.
        """
        if isinstance(expr, ast.Name):
            local = self.classes(path).get(expr.id)
            if local is not None:
                return path, local
            ref = self.symbol_bindings(path).get(expr.id)
            if ref is not None:
                found = self.classes(ref.file).get(ref.name)
                return (ref.file, found) if found is not None else None
            return None
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            module = self.module_bindings(path).get(expr.value.id)
            if module is not None:
                found = self.classes(module).get(expr.attr)
                return (module, found) if found is not None else None
        return None


def rebound_names(tree: ast.Module) -> frozenset[str]:
    """Names assigned, deleted, or taken as parameters anywhere in a module.

    A name rebound anywhere may not refer to what its import bound, so the
    checks that resolve names through imports leave it alone.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return frozenset(names)


def _module_level(tree: ast.Module) -> list[ast.stmt]:
    """Top-level statements, descending into ``if``/``try`` blocks.

    A name bound under ``if TYPE_CHECKING`` or ``try: import x`` is still bound
    on some path, so it counts as defined — the checks must never report a
    name a module *can* provide.
    """
    out: list[ast.stmt] = []
    stack = list(tree.body)
    while stack:
        stmt = stack.pop(0)
        out.append(stmt)
        if isinstance(stmt, ast.If):
            stack.extend([*stmt.body, *stmt.orelse])
        elif isinstance(stmt, ast.Try):
            stack.extend(
                [*stmt.body, *stmt.orelse, *stmt.finalbody,
                 *(s for h in stmt.handlers for s in h.body)]
            )
        elif isinstance(stmt, ast.With):
            stack.extend(stmt.body)
    return out


def _bound_by(stmt: ast.stmt) -> set[str]:
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {stmt.name}
    if isinstance(stmt, ast.Import | ast.ImportFrom):
        return {a.asname or a.name.split(".")[0] for a in stmt.names if a.name != "*"}
    names: set[str] = set()
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def _is_dynamic(tree: ast.Module) -> bool:
    """Whether the module can bind names the AST does not show."""
    for stmt in _module_level(tree):
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "__getattr__":
            return True
        if isinstance(stmt, ast.ImportFrom) and any(a.name == "*" for a in stmt.names):
            return True
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("globals", "exec")
        ):
            return True
    return False
