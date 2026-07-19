"""accounts feature operations (unimplemented stubs)."""

from __future__ import annotations


def validate_username(u: str) -> bool:
    """True iff u is 3-20 chars, starts with a lowercase letter, and contains only lowercase letters, digits, or underscores."""
    raise NotImplementedError


def password_strength(p: str) -> str:
    """Score p one point each for len>=8, an uppercase letter, a digit, and a non-alphanumeric char; return "weak" (score<=1), "medium" (2-3), or "strong" (4)."""
    raise NotImplementedError


def mask_email(e: str) -> str:
    """Mask the local part of an email as first char + "***" + last char (e.g. "alice@shop.com" -> "a***e@shop.com"); ValueError if there is no "@" or the local part is empty."""
    raise NotImplementedError


def initials(full_name: str) -> str:
    """Return the dotted uppercase initials of a full name, e.g. "john ronald tolkien" -> "J.R.T."; ValueError on an empty/whitespace name."""
    raise NotImplementedError


def signup_greeting(name: str) -> str:
    """Return "Welcome, <Name Title-Cased>!" for the new account, e.g. "mary anne" -> "Welcome, Mary Anne!"."""
    raise NotImplementedError


def is_adult(birth_year: int, current_year: int) -> bool:
    """True iff current_year - birth_year >= 18; ValueError if birth_year is after current_year."""
    raise NotImplementedError


def login_throttle_delay(attempts: int) -> int:
    """Seconds to wait before the next sign-in attempt: 0 for fewer than 3 failed attempts, else 2**(attempts-3) capped at 60; ValueError on negative attempts."""
    raise NotImplementedError
