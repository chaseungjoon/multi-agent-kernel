"""dictkit operations (unimplemented stubs)."""

from __future__ import annotations


def invert(d: dict) -> dict:
    """Return a new dict mapping each value back to its key."""
    raise NotImplementedError


def merge(a: dict, b: dict) -> dict:
    """Shallow-merge two dicts into a new one; keys in b win."""
    raise NotImplementedError


def pick(d: dict, keys: list) -> dict:
    """Return a new dict with only the given keys that exist in d."""
    raise NotImplementedError


def omit(d: dict, keys: list) -> dict:
    """Return a new dict without the given keys."""
    raise NotImplementedError


def get_in(d: dict, path: list) -> object:
    """Follow a list of keys into nested dicts; return None if any step is missing."""
    raise NotImplementedError


def key_of_max(d: dict) -> object:
    """Return the key whose value is largest (non-empty dict)."""
    raise NotImplementedError


def count_values(d: dict) -> dict:
    """Count how many keys map to each distinct value."""
    raise NotImplementedError


def deep_keys(d: dict) -> list:
    """Return all keys, recursing into nested dicts, sorted ascending."""
    raise NotImplementedError


def zip_dict(keys: list, values: list) -> dict:
    """Build a dict from parallel keys and values lists."""
    raise NotImplementedError


def max_value(d: dict) -> object:
    """Return the largest value in a non-empty dict."""
    raise NotImplementedError
