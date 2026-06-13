"""seqkit operations (unimplemented stubs)."""

from __future__ import annotations


def windowed(items: list, size: int) -> list:
    """All consecutive sliding windows of the given size over items."""
    raise NotImplementedError


def chunk(items: list, size: int) -> list:
    """Split items into consecutive chunks of the given size (last may be shorter)."""
    raise NotImplementedError


def flatten_deep(nested: list) -> list:
    """Recursively flatten arbitrarily nested lists into a single flat list."""
    raise NotImplementedError


def rotate(items: list, k: int) -> list:
    """Rotate items left by k positions (negative k rotates right)."""
    raise NotImplementedError


def pairwise(items: list) -> list:
    """Return consecutive overlapping pairs (a,b),(b,c),... as a list of tuples."""
    raise NotImplementedError


def dedupe(items: list) -> list:
    """Order-preserving removal of duplicate elements."""
    raise NotImplementedError


def frequencies(items: list) -> dict:
    """Map each distinct element to the number of times it appears."""
    raise NotImplementedError


def partition_even_odd(nums: list) -> tuple:
    """Split integers into (evens, odds), preserving order within each group."""
    raise NotImplementedError


def interleave(a: list, b: list) -> list:
    """Interleave two lists element by element, appending any leftover tail."""
    raise NotImplementedError


def run_length(items: list) -> list:
    """Run-length encode a list into (value, run_length) pairs."""
    raise NotImplementedError
