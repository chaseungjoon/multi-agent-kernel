"""Per-operation specification — passes once each stub is implemented correctly."""

import pytest

from toolkit import numbers, sequences, strings


def test_upper():
    assert strings.upper("hi") == "HI"


def test_reverse():
    assert strings.reverse("abc") == "cba"


def test_count_vowels():
    assert strings.count_vowels("Hello") == 2
    assert strings.count_vowels("xyz") == 0


def test_add():
    assert numbers.add(2, 3) == 5


def test_factorial():
    assert numbers.factorial(4) == 24
    assert numbers.factorial(0) == 1


def test_factorial_negative_raises():
    with pytest.raises(ValueError):
        numbers.factorial(-1)


def test_is_prime():
    assert numbers.is_prime(7) is True
    assert numbers.is_prime(1) is False
    assert numbers.is_prime(9) is False


def test_unique():
    assert sequences.unique([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_maximum():
    assert sequences.maximum([3, 9, 2]) == 9


def test_maximum_empty_raises():
    with pytest.raises(ValueError):
        sequences.maximum([])


def test_first():
    assert sequences.first([7, 8, 9]) == 7


def test_first_empty_raises():
    with pytest.raises(ValueError):
        sequences.first([])
