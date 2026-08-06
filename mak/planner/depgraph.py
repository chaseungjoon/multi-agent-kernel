"""Static dependency graph over node-store fragments, for plan validation.

The planner's ``depends_on`` edges are LLM guesses. MAK owns the AST, so it can
build a shallow *reference* graph — which node calls, imports, or otherwise
references which — directly from the committed node store, and use it to
*validate* a plan (see :mod:`mak.planner.validation`): a task that rewrites a node
another task's node references should generally depend on it.

The extraction is deliberately shallow and conservative — **when a reference
cannot be confidently resolved to a defining node, no edge is produced**. It is a
structural aid to the human-in-the-loop reviewer, not a call-graph analyzer. It is
intentionally *separate* from :mod:`mak.conflict_detector` (whose extractors are
test-pinned and drop two things this graph needs: the receiver name of an
attribute reference, and non-call value references such as a function passed as an
argument).

Node-id anatomy (see :mod:`mak.node_store.ingestion`): ``file.py::kind::name`` with
kinds ``function``/``method`` (name ``Class.method``)/``class``/``module_header``/
``module_body``/``class_body``; a bare ``file.py`` id is a whole-file node.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mak.core.types import NodeId

if TYPE_CHECKING:
    from mak.node_store.store import NodeStore

# Kinds whose source we scan for imports when building a file's import table.
_HEADER_KINDS = frozenset({"", "module_header", "module_body"})
# Kinds that can *reference* other nodes (the reference sources).
_SYMBOL_KINDS = frozenset({"", "function", "method", "class"})


@dataclass(frozen=True, slots=True)
class DepGraph:
    """A shallow reference graph over node-store nodes.

    ``references`` maps each symbol node to the set of nodes it references
    (calls/uses). ``definers`` maps a short symbol name — and, for methods, the
    qualified ``Class.method`` name too — to the node(s) that define it.
    """

    references: dict[NodeId, frozenset[NodeId]]
    definers: dict[str, tuple[NodeId, ...]]


@dataclass(frozen=True, slots=True)
class _ImportBinding:
    """A name a file's imports bind, resolved to the file(s) it may point at."""

    symbol_file: str | None  # from-import: file the imported symbol is defined in
    imported_name: str | None  # from-import: the imported short name
    module_file: str | None  # file the binding refers to when used as a module


def _parse_id(node_id: NodeId) -> tuple[str, str, str]:
    """Return ``(file_path, kind, name)``; kind/name are ``""`` for a whole-file id."""
    text = str(node_id)
    if "::" not in text:
        return text, "", ""
    file_path, kind, name = text.split("::", 2)
    return file_path, kind, name


def _short(name: str) -> str:
    """Strip an ingestion ``#n`` disambiguation suffix from a symbol name."""
    return name.split("#", 1)[0]


def _module_to_file(dotted: str, files: frozenset[str]) -> str | None:
    """Resolve an absolute dotted module to a file: exact, then unique suffix."""
    exact = dotted.replace(".", "/") + ".py"
    if exact in files:
        return exact
    last = dotted.split(".")[-1]
    matches = [
        f for f in files if f == f"{last}.py" or f.endswith(f"/{last}.py")
    ]
    return matches[0] if len(matches) == 1 else None


def _relative_dir(fp: str, level: int) -> list[str]:
    """Return the package directory (as path parts) for a relative import."""
    parts = fp.split("/")[:-1]  # drop the filename
    ascend = level - 1  # ``from .`` = current package; ``from ..`` = parent
    return parts[: len(parts) - ascend] if 0 <= ascend <= len(parts) else parts


def _dir_join(parts: list[str], name: str, files: frozenset[str]) -> str | None:
    candidate = "/".join([*parts, name]) + ".py"
    return candidate if candidate in files else None


class _DepGraphBuilder:
    """Builds the definer index, per-file import tables, and reference edges."""

    def __init__(self, sources: Mapping[NodeId, str]) -> None:
        self._sources = sources
        self._file_of: dict[NodeId, str] = {}
        self._definers: dict[str, list[NodeId]] = {}
        self._classes_by_file: dict[str, set[str]] = {}
        self._symbol_nodes: list[NodeId] = []
        self._index_definers()
        self._files = frozenset(self._file_of.values())
        self._imports = self._build_import_tables()

    # -- definer index ----------------------------------------------------

    def _index_definers(self) -> None:
        for node_id, source in self._sources.items():
            file_path, kind, name = _parse_id(node_id)
            self._file_of[node_id] = file_path
            if kind in _SYMBOL_KINDS:
                self._symbol_nodes.append(node_id)
            if kind == "function":
                self._add_definer(_short(name), node_id)
            elif kind == "class":
                self._add_class(file_path, _short(name), node_id)
            elif kind == "method":
                qualified = _short(name)
                self._add_definer(qualified.rsplit(".", 1)[-1], node_id)
                self._add_definer(qualified, node_id)
            elif kind == "":
                self._index_whole_file(node_id, file_path, source)

    def _add_definer(self, key: str, node_id: NodeId) -> None:
        self._definers.setdefault(key, []).append(node_id)

    def _add_class(self, file_path: str, class_name: str, node_id: NodeId) -> None:
        self._add_definer(class_name, node_id)
        self._classes_by_file.setdefault(file_path, set()).add(class_name)

    def _index_whole_file(self, node_id: NodeId, file_path: str, source: str) -> None:
        """Index every top-level symbol a whole-file node defines."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return
        for stmt in tree.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self._add_definer(stmt.name, node_id)
            elif isinstance(stmt, ast.ClassDef):
                self._add_class(file_path, stmt.name, node_id)
                for member in stmt.body:
                    if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                        self._add_definer(member.name, node_id)
                        self._add_definer(f"{stmt.name}.{member.name}", node_id)

    # -- import tables ----------------------------------------------------

    def _build_import_tables(self) -> dict[str, dict[str, _ImportBinding]]:
        tables: dict[str, dict[str, _ImportBinding]] = {}
        for node_id, source in self._sources.items():
            file_path, kind, _name = _parse_id(node_id)
            if kind not in _HEADER_KINDS:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            table = tables.setdefault(file_path, {})
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self._record_plain_import(table, node)
                elif isinstance(node, ast.ImportFrom):
                    self._record_from_import(table, node, file_path)
        return tables

    def _record_plain_import(
        self, table: dict[str, _ImportBinding], node: ast.Import
    ) -> None:
        for alias in node.names:
            if alias.asname:
                binding = alias.asname
                module_file = _module_to_file(alias.name, self._files)
            else:
                binding = alias.name.split(".")[0]
                module_file = _module_to_file(binding, self._files)
            table[binding] = _ImportBinding(None, None, module_file)

    def _record_from_import(
        self, table: dict[str, _ImportBinding], node: ast.ImportFrom, fp: str
    ) -> None:
        module = node.module or ""
        for alias in node.names:
            binding = alias.asname or alias.name
            if node.level:
                base = _relative_dir(fp, node.level)
                symbol_file = (
                    _dir_join(base, module.split(".")[0], self._files)
                    if module else None
                )
                module_file = _dir_join(base, alias.name, self._files)
            else:
                symbol_file = _module_to_file(module, self._files) if module else None
                dotted = f"{module}.{alias.name}" if module else alias.name
                module_file = _module_to_file(dotted, self._files)
            table[binding] = _ImportBinding(symbol_file, alias.name, module_file)

    # -- reference edges --------------------------------------------------

    def _resolve_references(self, node_id: NodeId) -> frozenset[NodeId]:
        try:
            tree = ast.parse(self._sources[node_id])
        except SyntaxError:
            return frozenset()
        file_path = self._file_of[node_id]
        bare, attrs = _collect_references(tree)
        result: set[NodeId] = set()
        for name in bare:
            result.update(self._resolve_bare(name, file_path, node_id))
        for receiver, attr in attrs:
            result.update(self._resolve_attr(receiver, attr, file_path, node_id))
        result.discard(node_id)
        return frozenset(result)

    def _definers_in(self, key: str, file_path: str, exclude: NodeId) -> list[NodeId]:
        return [
            d for d in self._definers.get(key, ())
            if self._file_of[d] == file_path and d != exclude
        ]

    def _resolve_bare(
        self, name: str, file_path: str, node_id: NodeId
    ) -> list[NodeId]:
        same_file = self._definers_in(name, file_path, node_id)
        if same_file:
            return same_file  # a same-file definer wins over any import
        binding = self._imports.get(file_path, {}).get(name)
        if binding and binding.symbol_file is not None:
            key = binding.imported_name or name
            return self._definers_in(key, binding.symbol_file, node_id)
        return []

    def _resolve_attr(
        self, receiver: str, attr: str, file_path: str, node_id: NodeId
    ) -> list[NodeId]:
        binding = self._imports.get(file_path, {}).get(receiver)
        if binding is not None and binding.module_file is not None:
            return self._definers_in(attr, binding.module_file, node_id)
        if receiver in self._classes_by_file.get(file_path, set()):
            return self._definers_in(f"{receiver}.{attr}", file_path, node_id)
        return []

    def build(self) -> DepGraph:
        references = {
            node_id: self._resolve_references(node_id)
            for node_id in self._symbol_nodes
        }
        definers = {key: tuple(nodes) for key, nodes in self._definers.items()}
        return DepGraph(references=references, definers=definers)


def _collect_references(tree: ast.AST) -> tuple[set[str], set[tuple[str, str]]]:
    """Collect bare loaded names and ``receiver.attr`` pairs from an AST."""
    bare: set[str] = set()
    attrs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            bare.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attrs.add((node.value.id, node.attr))
    return bare, attrs


def _module_to_file_strict(dotted: str, files: frozenset[str]) -> str | None:
    """Resolve an absolute dotted module by its **whole** path tail, or not at all.

    ``pkg.mod`` resolves to ``pkg/mod.py`` and to ``src/pkg/mod.py``, but never to
    ``other/mod.py``. Unlike :func:`_module_to_file` there is no last-segment
    fallback, which is what keeps a third-party import from landing on a repo file
    that merely ends the same way.
    """
    tail = dotted.replace(".", "/") + ".py"
    if tail in files:
        return tail
    matches = [f for f in files if f.endswith(f"/{tail}")]
    return matches[0] if len(matches) == 1 else None


def resolve_module_file(
    dotted: str,
    files: frozenset[str],
    *,
    from_file: str | None = None,
    level: int = 0,
    strict: bool = False,
) -> str | None:
    """Resolve an import's module to a file in ``files``, or None if it is external.

    ``level`` is ``ast.ImportFrom.level``: 0 for an absolute import, and ≥1 for a
    relative one, which resolves against ``from_file``'s package directory.

    ``strict`` requires an absolute import's **whole** dotted tail to match the
    file path. The loose default resolves by unique *last segment*, which is right
    for plan validation — a wrong edge there is a soft finding a human reviews —
    and wrong for anything that gates. Under the loose rule
    ``from PyInstaller.__main__ import run`` resolved to a repo's own
    ``editor/__main__.py``, and a defect check built on that would have told an
    agent to "fix" a correct third-party import. Relative imports are already
    exact and ``strict`` does not change them.

    Public because the post-wave cross-module check needs the same resolution this
    module already does — one implementation, so the two cannot drift into
    disagreeing about which file an import names.
    """
    parts = [p for p in dotted.split(".") if p]
    if not level:
        if not parts:
            return None
        return (
            _module_to_file_strict(dotted, files)
            if strict
            else _module_to_file(dotted, files)
        )
    if from_file is None:
        return None
    base = _relative_dir(from_file, level)
    return _dir_join([*base, *parts[:-1]], parts[-1], files) if parts else None


def build_dep_graph(sources: Mapping[NodeId, str]) -> DepGraph:
    """Build a :class:`DepGraph` from a ``{node_id: source}`` mapping."""
    return _DepGraphBuilder(sources).build()


def dep_graph_from_store(node_store: NodeStore) -> DepGraph:
    """Build a :class:`DepGraph` from the node store's committed inventory."""
    sources = {
        node_id: node_store.get_node(node_id).source
        for node_id in node_store.list_nodes()
    }
    return build_dep_graph(sources)
