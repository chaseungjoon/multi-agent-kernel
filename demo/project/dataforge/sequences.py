"""Sequence helpers.

Each function is an unimplemented stub. Implement it to match its docstring
(``tests/test_sequences.py`` is the executable specification).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")


def chunk(items: list[T], size: int) -> list[list[T]]:
    """Split ``items`` into consecutive chunks of length ``size``.

    The final chunk may be shorter. Raise ``ValueError`` if ``size`` is less
    than 1.

    Examples:
        >>> chunk([1, 2, 3, 4, 5], 2)
        [[1, 2], [3, 4], [5]]
        >>> chunk([], 3)
        []
    """
    raise NotImplementedError


def flatten(nested: Iterable[Iterable[T]]) -> list[T]:
    """Flatten one level of nesting into a single list.

    Examples:
        >>> flatten([[1, 2], [3], [4, 5]])
        [1, 2, 3, 4, 5]
        >>> flatten([])
        []
    """
    raise NotImplementedError


def dedupe(items: Iterable[T]) -> list[T]:
    """Return ``items`` with duplicates removed, preserving first-seen order.

    Examples:
        >>> dedupe([3, 1, 3, 2, 1])
        [3, 1, 2]
    """
    raise NotImplementedError


def partition(
    items: Iterable[T], predicate: Callable[[T], bool]
) -> tuple[list[T], list[T]]:
    """Split ``items`` into ``(matching, non_matching)`` by ``predicate``.

    Examples:
        >>> partition([1, 2, 3, 4], lambda n: n % 2 == 0)
        ([2, 4], [1, 3])
    """
    raise NotImplementedError
