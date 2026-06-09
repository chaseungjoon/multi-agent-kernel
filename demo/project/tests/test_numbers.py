"""Specification for dataforge.numbers — passes once the stubs are implemented."""

import pytest

from dataforge import numbers


def test_clamp_above_range():
    assert numbers.clamp(12, 0, 10) == 10


def test_clamp_below_range():
    assert numbers.clamp(-3, 0, 10) == 0


def test_clamp_within_range():
    assert numbers.clamp(5, 0, 10) == 5


def test_clamp_rejects_inverted_bounds():
    with pytest.raises(ValueError):
        numbers.clamp(5, 10, 0)


def test_mean():
    assert numbers.mean([2, 4, 6]) == 4.0


def test_mean_empty_raises():
    with pytest.raises(ValueError):
        numbers.mean([])


def test_median_odd_length():
    assert numbers.median([3, 1, 2]) == 2.0


def test_median_even_length():
    assert numbers.median([1, 2, 3, 4]) == 2.5


def test_median_empty_raises():
    with pytest.raises(ValueError):
        numbers.median([])


def test_running_total():
    assert numbers.running_total([1, 2, 3]) == [1, 3, 6]


def test_running_total_empty():
    assert numbers.running_total([]) == []
