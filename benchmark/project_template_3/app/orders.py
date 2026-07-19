"""orders feature operations (unimplemented stubs)."""

from __future__ import annotations


def order_number(seq: int, year: int) -> str:
    """Build an order number "ORD-<year>-<seq zero-padded to 6>", e.g. (42, 2026) -> "ORD-2026-000042"; ValueError if seq <= 0."""
    raise NotImplementedError


def next_status(status: str) -> str:
    """Advance an order status along placed -> paid -> shipped -> delivered; ValueError for "delivered" or any unknown status."""
    raise NotImplementedError


def order_total(subtotal: int, tax_bp: int, ship: int) -> int:
    """Total = subtotal + floor(subtotal*tax_bp/10000) + ship, where tax_bp is basis points (875 = 8.75%); ValueError on any negative input."""
    raise NotImplementedError


def confirmation_line(order_no: str, total_cents: int) -> str:
    """Return "Order <no> confirmed - total $D.DD", e.g. ("ORD-2026-000042", 11375) -> "Order ORD-2026-000042 confirmed - total $113.75"."""
    raise NotImplementedError


def estimated_delivery(day_of_week: int, transit_days: int) -> int:
    """Delivery day of week: (day_of_week + transit_days) % 7 with days numbered 0-6; ValueError if day_of_week is outside 0-6 or transit_days is negative."""
    raise NotImplementedError


def cancel_allowed(status: str) -> bool:
    """True iff an order in this status may still be cancelled ("placed" or "paid"); ValueError for a status outside placed/paid/shipped/delivered."""
    raise NotImplementedError


def split_shipments(items: list, box_size: int) -> list:
    """Chunk items into boxes of at most box_size, preserving order, e.g. ([1,2,3,4,5], 2) -> [[1,2],[3,4],[5]]; ValueError if box_size <= 0."""
    raise NotImplementedError


def refund_amount(total: int, days: int, window: int) -> int:
    """Refund policy: full refund within `window` days of purchase, half (floored) within twice the window, else 0; ValueError if window <= 0."""
    raise NotImplementedError
