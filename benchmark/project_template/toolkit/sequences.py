"""Sequence operations (unimplemented stubs)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def unique(items: Sequence[T]) -> list[T]:
    """Return ``items`` with duplicates removed, preserving first-seen order.

    Examples:
        >>> unique([3, 1, 3, 2, 1])
        [3, 1, 2]
    """
    raise NotImplementedError


def maximum(items: Sequence[T]) -> T:
    """Return the largest item. Raise ``ValueError`` if ``items`` is empty.

    Examples:
        >>> maximum([3, 9, 2])
        9
    """
    raise NotImplementedError


def first(items: Sequence[T]) -> T:
    """Return the first item. Raise ``ValueError`` if ``items`` is empty.

    Examples:
        >>> first([7, 8, 9])
        7
    """
    raise NotImplementedError
