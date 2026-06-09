"""Specification for dataforge.sequences — passes once the stubs are implemented."""

import pytest

from dataforge import sequences


def test_chunk_splits_with_short_final_chunk():
    assert sequences.chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_chunk_empty():
    assert sequences.chunk([], 3) == []


def test_chunk_rejects_nonpositive_size():
    with pytest.raises(ValueError):
        sequences.chunk([1, 2, 3], 0)


def test_flatten_one_level():
    assert sequences.flatten([[1, 2], [3], [4, 5]]) == [1, 2, 3, 4, 5]


def test_flatten_empty():
    assert sequences.flatten([]) == []


def test_dedupe_preserves_first_seen_order():
    assert sequences.dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_partition_by_predicate():
    assert sequences.partition([1, 2, 3, 4], lambda n: n % 2 == 0) == ([2, 4], [1, 3])
