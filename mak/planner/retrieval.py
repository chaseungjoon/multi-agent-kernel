"""Choose which files a ``retrieval`` plan sees in detail before it asks.

The first ``retrieval`` prompt shows the repository tree plus a few files at
level 1 — the *seeds*. Seeding is deterministic and local: a
:class:`Retriever` scores files against the task text and fills its share of
the budget. :class:`LexicalRetriever` is the only implementation today; an
embedding retriever can replace it later without touching the planner.

Lexical scoring, per node:

- the task names the node's symbol exactly, or its file path → **10**;
- a task term equals a whole path segment of the node's file → **4**;
- each sub-token the task shares with the node's name → its **IDF** over all
  symbol names, so ``get`` (in thousands of names) is worth almost nothing and
  ``invoice`` (in six) a lot.

A file scores the sum of its best five nodes. The top five files then lend half
their score to the files they reference or are referenced by, because the file
a task names is rarely the only one its plan must touch.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from mak.planner.inventory import InventoryView

EXACT_SCORE = 10.0
SEGMENT_SCORE = 4.0
NEIGHBOUR_FACTOR = 0.5
_BEST_NODES = 5
_TOP_FILES = 5
_MIN_TERM = 3

# Words that say nothing about *where* a change goes. Deliberately short: a
# domain word wrongly listed here can never be matched.
STOP_WORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "into", "onto", "not",
    "all", "any", "are", "was", "has", "have", "its", "use", "uses", "using",
    "add", "adds", "get", "gets", "set", "sets", "new", "make", "fix", "fixes",
    "update", "updates", "change", "changes", "remove", "rename", "support",
    "should", "must", "when", "then", "also", "every", "each", "per", "via",
    "self", "cls", "init", "test", "tests", "def", "class", "return", "none",
    "true", "false", "import", "file", "files", "function", "method", "code",
    "module", "value", "values", "call", "calls", "caller", "callers",
})

_SPAN = re.compile(r"`([^`]+)`")
_DOTTED = re.compile(r"[A-Za-z_][\w]*(?:[./][A-Za-z_][\w]*)+(?:\.py)?")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUBTOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+")


@dataclass(frozen=True, slots=True)
class SeedResult:
    """The files to pre-expand, in order, with why each was chosen."""

    files: tuple[str, ...]
    terms: tuple[str, ...]
    scores: tuple[tuple[str, float], ...]


class Retriever(Protocol):
    """Picks the files a ``retrieval`` plan starts out seeing in detail."""

    def seed(self, task: str, view: InventoryView, budget_tokens: int) -> SeedResult:
        """Return files whose level-1 renderings together fit ``budget_tokens``."""
        ...


def split_identifier(name: str) -> list[str]:
    """Split on snake_case, camelCase and digits; case-fold; drop short parts."""
    parts: list[str] = []
    for chunk in re.split(r"[_\W]+", name):
        parts.extend(match.group(0).lower() for match in _SUBTOKEN.finditer(chunk))
    return [p for p in parts if len(p) >= _MIN_TERM and not p.isdigit()]


def extract_terms(task: str) -> tuple[list[str], list[str]]:
    """Return ``(whole terms, sub-tokens)`` from a task description.

    Whole terms are backticked spans, dotted names and ``/`` paths, plus every
    identifier, case-folded; they are what an exact match compares against.
    Sub-tokens are every whole term split into its words. Both drop stop words
    and anything shorter than three characters.
    """
    whole: list[str] = []
    for match in _SPAN.finditer(task):
        whole.append(match.group(1).strip())
    whole.extend(match.group(0) for match in _DOTTED.finditer(task))
    whole.extend(match.group(0) for match in _IDENT.finditer(task))
    originals = [
        term for term in whole
        if len(term) >= _MIN_TERM and term.lower() not in STOP_WORDS
    ]
    # Split before case-folding: "getUserName" is three words, "getusername" one.
    subtokens = _unique(
        part for term in originals for part in split_identifier(term)
        if part not in STOP_WORDS
    )
    return _unique(term.lower() for term in originals), subtokens


def _unique(items: Iterable[str]) -> list[str]:
    """Return ``items`` without repeats, first occurrence first."""
    return list(dict.fromkeys(items))


class LexicalRetriever:
    """Deterministic seeding from the words of the task (see the module docs)."""

    def seed(self, task: str, view: InventoryView, budget_tokens: int) -> SeedResult:
        """Score every file against ``task`` and take the best that fit."""
        whole, subtokens = extract_terms(task)
        if not whole and not subtokens:
            return SeedResult((), (), ())
        idf = _idf(view)
        scores = {
            path: _file_score(path, view, set(whole), subtokens, idf)
            for path in view.files
        }
        _spread_to_neighbours(scores, view)
        ranked = sorted(
            ((path, score) for path, score in scores.items() if score > 0),
            key=lambda item: (-item[1], item[0]),
        )
        chosen: list[str] = []
        used = 0
        for path, _score in ranked:
            cost = view.file_tokens(path, budget_tokens)
            if used + cost > budget_tokens:
                continue
            chosen.append(path)
            used += cost
        return SeedResult(
            files=tuple(chosen),
            terms=tuple(whole),
            scores=tuple((path, round(score, 4)) for path, score in ranked),
        )


def _symbol(name: str) -> str:
    """Return the short symbol of a node name (``Class.method#2`` → ``method``)."""
    return name.split("#", 1)[0].rsplit(".", 1)[-1]


def _idf(view: InventoryView) -> dict[str, float]:
    """Return each sub-token's inverse document frequency over symbol names."""
    frequency: dict[str, int] = {}
    total = 0
    for path in view.files:
        entry = view.file(path)
        assert entry is not None
        for node in entry.nodes:
            if not node.name or node.name.startswith("__"):
                continue
            total += 1
            for part in set(split_identifier(_symbol(node.name))):
                frequency[part] = frequency.get(part, 0) + 1
    return {
        part: math.log((1 + total) / (1 + count)) + 1.0
        for part, count in frequency.items()
    }


def _file_score(
    path: str,
    view: InventoryView,
    whole: set[str],
    subtokens: list[str],
    idf: dict[str, float],
) -> float:
    """Return the sum of the file's best node scores."""
    entry = view.file(path)
    assert entry is not None
    stem = path.lower().removesuffix(".py")
    # "pkg/billing/invoice.py" is named by the path, by "pkg/billing/invoice"
    # and by the dotted module "pkg.billing.invoice".
    path_hit = bool({path.lower(), stem, stem.replace("/", ".")} & whole)
    segments = set(stem.split("/"))
    segment_hit = bool(segments & (whole | set(subtokens)))
    base = EXACT_SCORE if path_hit else 0.0
    base += SEGMENT_SCORE if segment_hit else 0.0
    node_scores = []
    for node in entry.nodes:
        symbol = _symbol(node.name).lower()
        score = base
        if symbol and (symbol in whole or node.name.lower() in whole):
            score += EXACT_SCORE
        parts = set(split_identifier(symbol))
        score += sum(idf.get(part, 0.0) for part in subtokens if part in parts)
        node_scores.append(score)
    node_scores.sort(reverse=True)
    return sum(node_scores[:_BEST_NODES])


def _spread_to_neighbours(scores: dict[str, float], view: InventoryView) -> None:
    """Give each top file's 1-hop neighbours half of its score."""
    top = sorted(
        ((path, score) for path, score in scores.items() if score > 0),
        key=lambda item: (-item[1], item[0]),
    )[:_TOP_FILES]
    for path, score in top:
        for neighbour in sorted(view.neighbour_files(path)):
            if neighbour in scores:
                scores[neighbour] += NEIGHBOUR_FACTOR * score
