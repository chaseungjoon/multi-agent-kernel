"""Numeric helpers.

Each function is an unimplemented stub. Implement it to match its docstring
(``tests/test_numbers.py`` is the executable specification).
"""

from __future__ import annotations

from collections.abc import Sequence


def clamp(value: float, low: float, high: float) -> float:
    """Constrain ``value`` to the inclusive range ``[low, high]``.

    Raise ``ValueError`` if ``low`` is greater than ``high``.

    Examples:
        >>> clamp(12, 0, 10)
        10
        >>> clamp(-3, 0, 10)
        0
        >>> clamp(5, 0, 10)
        5
    """
    raise NotImplementedError


def mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean of ``values``.

    Raise ``ValueError`` if ``values`` is empty.

    Examples:
        >>> mean([2, 4, 6])
        4.0
    """
    raise NotImplementedError


def median(values: Sequence[float]) -> float:
    """Return the median of ``values``.

    For an even number of values, return the average of the two middle values.
    Raise ``ValueError`` if ``values`` is empty.

    Examples:
        >>> median([3, 1, 2])
        2.0
        >>> median([1, 2, 3, 4])
        2.5
    """
    raise NotImplementedError


def running_total(values: Sequence[float]) -> list[float]:
    """Return the cumulative sums of ``values`` (empty input yields an empty list).

    Examples:
        >>> running_total([1, 2, 3])
        [1, 3, 6]
        >>> running_total([])
        []
    """
    raise NotImplementedError
