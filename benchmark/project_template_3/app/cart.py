"""cart feature operations (unimplemented stubs)."""

from __future__ import annotations


def cart_total(prices: list) -> int:
    """Sum a list of integer cent prices (empty list -> 0); ValueError if any price is negative."""
    raise NotImplementedError


def add_item(items: list, item: str, max_items: int) -> list:
    """Return a NEW list with item appended; ValueError when the cart already holds max_items entries."""
    raise NotImplementedError


def item_counts(items: list) -> dict:
    """Count occurrences of each item, e.g. ['a','b','a'] -> {'a': 2, 'b': 1}."""
    raise NotImplementedError


def apply_coupon(total: int, code: str) -> int:
    """Apply coupon "SAVE10" (10% off) or "SAVE25" (25% off) to the total, floored to whole cents; ValueError for any other code."""
    raise NotImplementedError


def free_shipping_eligible(total_cents: int, threshold: int) -> bool:
    """True iff the cart total meets or exceeds the free-shipping threshold."""
    raise NotImplementedError


def remove_item(items: list, item: str) -> list:
    """Return a NEW list with the FIRST occurrence of item removed; ValueError if the item is not present."""
    raise NotImplementedError


def quantity_update(counts: dict, item: str, qty: int) -> dict:
    """Return a NEW dict with item's quantity set to qty (qty of 0 removes the item); ValueError on negative qty."""
    raise NotImplementedError
