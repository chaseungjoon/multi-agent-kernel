"""Shared events wiring; build fresh state for every lookup."""

from service import tenancy, submission, scheduling, leasing, lifecycle, operations


def _register_all() -> dict[str, object]:
    """Register feature handlers in a local table."""
    entries: dict[str, object] = {}
    register = entries.__setitem__
    return entries


def lookup(key: str) -> object:
    """Return a registered handler or raise KeyError."""
    return _register_all()[key]
