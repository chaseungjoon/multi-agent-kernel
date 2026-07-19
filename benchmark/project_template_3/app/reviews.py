"""reviews feature operations (unimplemented stubs)."""

from __future__ import annotations


def average_rating(ratings: list) -> float:
    """Mean rating rounded HALF UP to one decimal (e.g. [3,4,4] -> 3.7); ValueError on an empty list or any rating outside 1-5."""
    raise NotImplementedError


def star_histogram(ratings: list) -> dict:
    """Histogram of ratings with ALL keys 1-5 present (zero when absent); ValueError for any rating outside 1-5."""
    raise NotImplementedError


def contains_profanity(text: str, banned: list) -> bool:
    """True iff any banned word matches a WHOLE word of text, case-insensitively (substrings inside longer words do not count)."""
    raise NotImplementedError


def helpfulness(up: int, down: int) -> int:
    """Helpfulness percentage floored: up*100 // (up+down), and 0 when there are no votes; ValueError on negative votes."""
    raise NotImplementedError


def truncate_review(text: str, n: int) -> str:
    """Truncate text to at most n chars, replacing the tail with a single ellipsis character (U+2026) when shortened; ValueError if n < 1."""
    raise NotImplementedError


def verified_badge(purchased: bool, rating: int) -> str:
    """Return "Verified ★N" for a verified purchase else "★N" (N is the 1-5 rating); ValueError for a rating outside 1-5."""
    raise NotImplementedError


def sort_reviews(pairs: list) -> list:
    """Sort (helpfulness, review_id) pairs by helpfulness descending, then id ascending."""
    raise NotImplementedError
