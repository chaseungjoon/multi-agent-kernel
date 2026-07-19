"""shipping feature operations (unimplemented stubs)."""

from __future__ import annotations


def shipping_cost(weight_g: int, zone: int) -> int:
    """Cost in cents: zone base (1->500, 2->900, 3->1400) plus 100 per *started* kilogram; ValueError for an unknown zone or non-positive weight."""
    raise NotImplementedError


def normalize_postcode(s: str) -> str:
    """Remove all spaces and uppercase the postcode, e.g. " se1 9gf " -> "SE19GF"; ValueError if the result is empty or not alphanumeric."""
    raise NotImplementedError


def address_label(name: str, street: str, city: str, code: str) -> str:
    """Three-line label: NAME uppercased, then street, then "city code" joined by a space; ValueError on an empty name."""
    raise NotImplementedError


def delivery_window(days: int, express: bool) -> int:
    """Estimated delivery days: express halves the standard estimate (floored, minimum 1), otherwise unchanged; ValueError if days < 1."""
    raise NotImplementedError


def tracking_valid(code: str) -> bool:
    """True iff the tracking code is exactly two uppercase letters followed by nine digits (11 chars total); ValueError on an empty string."""
    raise NotImplementedError


def zone_for_country(country: str) -> int:
    """Shipping zone by country code (case-insensitive): us/ca -> 1, uk/de/fr -> 2, anything else -> 3; ValueError on an empty string."""
    raise NotImplementedError


def oversize_surcharge(weight_g: int, limit_g: int, per_kg_cents: int) -> int:
    """Surcharge for weight over the limit: per_kg_cents for every *started* kg over (0 when within the limit); ValueError if limit_g <= 0."""
    raise NotImplementedError
