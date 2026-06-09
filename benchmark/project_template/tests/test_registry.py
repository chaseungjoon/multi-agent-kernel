"""Dispatch specification — every operation must be registered in the shared table.

These are the tests that fail when a worktree merge drops or mangles a
``register(...)`` line in ``_register_all``: the operation never makes it into
``OPERATIONS``, so ``run(name, ...)`` raises ``KeyError``.
"""

import pytest

from toolkit import registry

EXPECTED = {
    "upper": (("hi",), "HI"),
    "reverse": (("abc",), "cba"),
    "count_vowels": (("Hello",), 2),
    "add": ((2, 3), 5),
    "factorial": ((4,), 24),
    "is_prime": ((7,), True),
    "unique": (([3, 1, 3, 2, 1],), [3, 1, 2]),
    "maximum": (([3, 9, 2],), 9),
    "first": (([7, 8, 9],), 7),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_operation_is_registered(name):
    assert name in registry.OPERATIONS, f"{name!r} was never registered"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_operation_dispatches_correctly(name):
    args, expected = EXPECTED[name]
    assert registry.run(name, *args) == expected
