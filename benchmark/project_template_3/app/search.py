"""search feature operations (unimplemented stubs)."""

from __future__ import annotations


def tokenize(q: str) -> list:
    """Lowercase the query and split it into alphanumeric tokens (punctuation and spaces separate tokens; empties dropped)."""
    raise NotImplementedError


def match_score(query_tokens: list, doc_tokens: list) -> int:
    """Number of DISTINCT query tokens that appear in the document tokens."""
    raise NotImplementedError


def highlight(text: str, term: str) -> str:
    """Wrap every case-insensitive occurrence of term in square brackets, preserving the original casing, e.g. ("The Cat sat on a cat", "cat") -> "The [Cat] sat on a [cat]"; ValueError on an empty term."""
    raise NotImplementedError


def suggest(prefix: str, vocab: list) -> list:
    """The first five vocabulary words that start with the prefix, sorted alphabetically."""
    raise NotImplementedError


def page_count(total: int, per_page: int) -> int:
    """Number of pages needed for `total` results at `per_page` per page (ceiling; 0 results -> 0 pages); ValueError if per_page <= 0 or total < 0."""
    raise NotImplementedError


def filter_by_price(items: list, lo: int, hi: int) -> list:
    """Keep (name, price) pairs whose price is within [lo, hi] inclusive, preserving order; ValueError if lo > hi."""
    raise NotImplementedError


def query_time_bucket(ms: int) -> str:
    """Bucket a query latency: < 100ms -> "fast", 100-499ms -> "ok", >= 500ms -> "slow"; ValueError on a negative latency."""
    raise NotImplementedError
