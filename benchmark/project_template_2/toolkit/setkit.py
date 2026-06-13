"""setkit operations (unimplemented stubs)."""

from __future__ import annotations


def jaccard(a: list, b: list) -> float:
    """Jaccard similarity |A∩B| / |A∪B| of two collections (1.0 if both empty)."""
    raise NotImplementedError


def symmetric_diff(a: list, b: list) -> list:
    """Sorted symmetric difference: elements in exactly one of a or b."""
    raise NotImplementedError


def is_subset(a: list, b: list) -> bool:
    """Whether every element of a is also in b."""
    raise NotImplementedError


def union_all(lists: list) -> list:
    """Sorted union of all elements across a list of lists."""
    raise NotImplementedError


def intersection_all(lists: list) -> list:
    """Sorted intersection common to every list (empty if no lists)."""
    raise NotImplementedError


def count_common(a: list, b: list) -> int:
    """Number of distinct elements present in both a and b."""
    raise NotImplementedError


def unique_to_first(a: list, b: list) -> list:
    """Sorted elements present in a but not in b."""
    raise NotImplementedError


def is_disjoint(a: list, b: list) -> bool:
    """Whether a and b share no elements."""
    raise NotImplementedError


def powerset_size(items: list) -> int:
    """Number of subsets of the set of distinct elements (2**n)."""
    raise NotImplementedError


def mode(items: list) -> object:
    """Most frequent element; on a tie return the smallest such element."""
    raise NotImplementedError
