"""payments feature operations (unimplemented stubs)."""

from __future__ import annotations


def luhn_valid(number: str) -> bool:
    """Luhn checksum over a digit string: double every second digit from the right (subtracting 9 when > 9) and require the sum % 10 == 0; ValueError on an empty or non-digit input."""
    raise NotImplementedError


def mask_card(number: str) -> str:
    """Mask a card number keeping only the last four digits: "**** **** **** 1234"; ValueError if fewer than 4 characters."""
    raise NotImplementedError


def processing_fee(cents: int, bp: int) -> int:
    """Fee = floor(cents * bp / 10000) where bp is basis points (290 = 2.90%); ValueError on any negative input."""
    raise NotImplementedError


def split_evenly(total: int, n: int) -> list:
    """Split total cents across n payers: the first (total % n) payers pay one cent more, e.g. (10, 3) -> [4, 3, 3]; ValueError if n <= 0."""
    raise NotImplementedError


def currency_to_cents(s: str) -> int:
    """Parse "$D.DD" into integer cents, e.g. "$12.34" -> 1234; ValueError unless the string is a $, digits, a dot, and exactly two decimal digits."""
    raise NotImplementedError


def is_expired(exp_month: int, exp_year: int, now_month: int, now_year: int) -> bool:
    """True iff the card (valid through the END of its expiry month) is expired at now_month/now_year; ValueError for any month outside 1-12."""
    raise NotImplementedError


def receipt_id(order_no: str, attempt: int) -> str:
    """Build a receipt id "<order_no>/RNN" with the attempt zero-padded to two digits, e.g. ("ORD-2026-000042", 3) -> "ORD-2026-000042/R03"; ValueError if attempt < 1."""
    raise NotImplementedError
