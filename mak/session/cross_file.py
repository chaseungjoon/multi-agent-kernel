"""Enrichment layer 4: nodes in other files that name a target's symbols."""

from __future__ import annotations

import ast
import re
from collections import Counter

from mak.core.types import NodeId
from mak.session.store_view import StoreView

# Match-quality bounds for the cross-file layer. Both are about *evidence*, not
# cost — the byte budget is the cost dial (``session.cross_file_context_bytes``).
# A symbol shorter than this is a word, not a name a relationship can be inferred
# from: ``run`` matches unrelated files wholesale.
MIN_SYMBOL_LEN = 4
# A symbol that appears in more than this many nodes says nothing about which of
# them is related to the target, so it is discarded entirely rather than dragging
# every match in behind it.
MAX_SYMBOL_MATCHES = 8

# Maximal word runs, which is both what the symbol index is keyed on and the
# test for whether a symbol *can* be: ``\bfoo\b`` matches exactly where ``foo``
# is one such run, so indexing the runs answers the same question. Node ids yield
# Python identifiers, so every symbol qualifies in practice — the ``fullmatch``
# check exists so an id that somehow carries punctuation is scanned by regex
# rather than silently missed by the index.
_WORD = re.compile(r"\w+")

CONTEXT_KEYS = ("write_source", "read_source", "read_api")

# One candidate: a node, its source, and the target symbols it mentions.
Candidate = tuple[NodeId, str, frozenset[str]]


def context_has(context: dict[str, str], node_id: NodeId) -> bool:
    """Whether a bundle's context already carries ``node_id`` under any key."""
    return any(f"{prefix}:{node_id}" in context for prefix in CONTEXT_KEYS)


class CrossFileIndex:
    """Find, rank and budget the cross-file nodes that mention a task's symbols.

    Owns the ``symbol -> node ids`` index, rebuilt only when the store's
    committed set moves, so one build serves every dispatch and retry of a wave.
    """

    def __init__(self, view: StoreView) -> None:
        self._view = view
        self._index: dict[str, list[NodeId]] = {}
        self._index_at: int = -1

    def add_references(
        self,
        target_nodes: list[NodeId],
        target_files: set[str],
        context: dict[str, str],
        budget: int,
    ) -> tuple[list[str], int]:
        """Add nodes in other files that mention a target symbol by name.

        Returns ``(keys added, nodes dropped for budget)``. Three things keep
        this layer honest, in a single pass over the store:

        - a symbol shorter than ``MIN_SYMBOL_LEN`` is a word, not evidence of a
          relationship;
        - a symbol matching more than ``MAX_SYMBOL_MATCHES`` nodes is not evidence
          either, and is discarded wholesale rather than node by node;
        - what survives is ranked by match count (most first, then smallest, then
          id) and added until ``budget`` bytes are spent.

        Past the budget an entry is **dropped**, not degraded to a digest as the
        dependency layer does: a caller's value *is* its call site, and a
        signature digest of a caller says nothing about how it calls.
        """
        symbols = {
            s for s in self._target_symbols(target_nodes) if len(s) >= MIN_SYMBOL_LEN
        }
        if not symbols or budget == 0:
            return [], 0
        candidates = self._scan_for_symbols(symbols, target_files, context)
        return _spend_budget(candidates, context, budget)

    def _scan_for_symbols(
        self,
        symbols: set[str],
        target_files: set[str],
        context: dict[str, str],
    ) -> list[Candidate]:
        r"""Each node mentioning one of ``symbols``, with the symbols it hit.

        Looked up in the inverted index rather than by regex-scanning every
        node's source: the byte budget caps what is *sent*, not what is scanned,
        so a per-dispatch scan would dominate enrichment on a large repo.

        The index keys a node under exactly the maximal ``\w+`` runs in its
        source, which is the same condition ``\bsymbol\b`` tests. A symbol that
        is not a plain identifier cannot be answered that way and falls back to
        the scan.
        """
        exotic = {s for s in symbols if not _WORD.fullmatch(s)}
        hits_by_node: dict[NodeId, set[str]] = {}
        if exotic:
            pattern = re.compile(
                r"\b(?:" + "|".join(re.escape(s) for s in sorted(exotic)) + r")\b"
            )
        index = self._symbol_index()
        for symbol in symbols - exotic:
            for node_id in index.get(symbol, ()):
                hits_by_node.setdefault(node_id, set()).add(symbol)

        found: list[Candidate] = []
        for xfile_id in self._view.store.list_nodes():
            if str(xfile_id).split("::", 1)[0] in target_files:
                continue  # same-file already handled in layer 3
            if context_has(context, xfile_id):
                continue
            hits = set(hits_by_node.get(xfile_id, ()))
            if exotic:
                source = self._view.source(xfile_id)
                if source:
                    hits |= set(pattern.findall(source))
            if not hits:
                continue
            source = self._view.source(xfile_id)
            if not source:
                continue
            found.append((xfile_id, source, frozenset(hits)))
        return found

    def _symbol_index(self) -> dict[str, list[NodeId]]:
        """Return the ``symbol -> node ids`` index, rebuilt when the store moves.

        Keyed on ``NodeStore.generation``, which changes exactly when the
        committed set does — so a commit mid-wave invalidates it without anyone
        having to know which nodes moved.
        """
        generation = self._view.store.generation
        if self._index_at != generation:
            self._index = self._build_symbol_index()
            self._index_at = generation
        return self._index

    def _build_symbol_index(self) -> dict[str, list[NodeId]]:
        """Index every committed node under each identifier its source contains."""
        index: dict[str, list[NodeId]] = {}
        for node_id in self._view.store.list_nodes():
            source = self._view.source(node_id)
            if not source:
                continue
            for token in set(_WORD.findall(source)):
                index.setdefault(token, []).append(node_id)
        return index

    def _target_symbols(self, target_nodes: list[NodeId]) -> set[str]:
        """Return the symbol names a task's targets define, for the layer-4 scan.

        A ``file::kind::name`` id contributes the rightmost segment of its
        qualified name ("apple" from "FruitManager.apple"). A bare-path
        *whole-file* id has no name segment at all — and whole-file grants are
        the normal shape once a plan folds a file's nodes together — so its
        symbols come from the file's committed nodes instead; otherwise a
        whole-file target would silently disable the entire layer.
        """
        symbols: set[str] = set()
        for node_id in target_nodes:
            parts = str(node_id).split("::")
            if len(parts) >= 3:
                symbols.add(symbol_of(parts[2]))
            else:
                symbols |= self._file_symbols(parts[0])
        return {s for s in symbols if s}

    def _file_symbols(self, file_path: str) -> set[str]:
        """Symbol names defined by a file's committed nodes, fragments or whole."""
        symbols: set[str] = set()
        for node_id in self._view.store.list_nodes(file_path):
            parts = str(node_id).split("::")
            if len(parts) >= 3:
                symbols.add(symbol_of(parts[2]))
        source = self._view.source(NodeId(file_path))
        if source is not None:
            symbols |= defined_symbol_names(source)
        return symbols


def _spend_budget(
    candidates: list[Candidate], context: dict[str, str], budget: int
) -> tuple[list[str], int]:
    """Discard over-broad symbols, rank what is left, and fill the budget."""
    counts: Counter[str] = Counter()
    for _node_id, _source, hits in candidates:
        counts.update(hits)
    broad = {s for s, n in counts.items() if n > MAX_SYMBOL_MATCHES}
    kept = [
        (node_id, source, hits - broad)
        for node_id, source, hits in candidates
        if hits - broad
    ]
    kept.sort(key=lambda c: (-len(c[2]), len(c[1]), str(c[0])))
    added: list[str] = []
    dropped = 0
    spent = 0
    for node_id, source, _hits in kept:
        if budget >= 0 and spent + len(source) > budget:
            dropped += 1
            continue  # a smaller node further down may still fit
        key = f"read_source:{node_id}"
        context[key] = source
        added.append(key)
        spent += len(source)
    return added, dropped


def symbol_of(qualified_name: str) -> str:
    """Short symbol name of a node id's name segment, without ingestion suffixes.

    ``FruitManager.apple#2`` -> ``apple``. The ``#n`` disambiguation suffix has to
    go: it is not a word character, so a regex built from it can never match.
    """
    return qualified_name.split("#", 1)[0].rsplit(".", 1)[-1]


def defined_symbol_names(source: str) -> set[str]:
    """Names a module defines that could be node ids: functions, classes, methods.

    Deliberately **not** module-level assignments. Ingestion only ever creates
    ``function`` / ``class`` / ``method`` nodes, so those names are exactly what a
    symbol-level target contributes to the cross-file scan, and a whole-file target
    must contribute the same set or the two disagree about one file depending on
    how it happens to be stored. Assignments would also match nearly every module
    through names like ``__all__``, turning one whole-file target into a bundle
    carrying every other module that declares one.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.ClassDef):
            names.add(stmt.name)
            stack.extend(stmt.body)  # methods are node ids too
    return names
