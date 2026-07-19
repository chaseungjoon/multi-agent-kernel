"""catalog feature operations (unimplemented stubs)."""

from __future__ import annotations


def slugify(title: str) -> str:
    """Lowercase the title, replace every non-alphanumeric char with a hyphen, collapse runs of hyphens, and strip leading/trailing hyphens."""
    raise NotImplementedError


def format_price(cents: int) -> str:
    """Format integer cents as "$D.DD", e.g. 1234 -> "$12.34" and 5 -> "$0.05"; ValueError on negative cents."""
    raise NotImplementedError


def apply_discount(cents: int, percent: int) -> int:
    """Return the price after the discount, floored to whole cents: cents*(100-percent)//100; ValueError unless 0 <= percent <= 100."""
    raise NotImplementedError


def in_stock(stock: int, qty: int) -> bool:
    """True iff stock covers the requested qty; ValueError if qty <= 0."""
    raise NotImplementedError


def sku(category: str, number: int) -> str:
    """Build a SKU from the first three characters of category uppercased, a hyphen, and the number zero-padded to five digits, e.g. ("shoes", 42) -> "SHO-00042"; ValueError on an empty category or non-positive number."""
    raise NotImplementedError


def star_bar(n: int) -> str:
    """Render an n-of-5 star bar using U+2605 (filled) then U+2606 (empty), e.g. 3 -> "★★★☆☆"; ValueError unless 0 <= n <= 5."""
    raise NotImplementedError


def list_page(items: list, page: int, size: int) -> list:
    """Return the 1-based page of items of the given size (may be empty past the end); ValueError if page < 1 or size < 1."""
    raise NotImplementedError


def bulk_price(cents: int, qty: int) -> int:
    """Per-unit price after volume discount, floored: 20% off for qty >= 50, 10% off for qty >= 10, else unchanged; ValueError if qty <= 0."""
    raise NotImplementedError
