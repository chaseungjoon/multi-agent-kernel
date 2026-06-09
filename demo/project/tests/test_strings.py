"""Specification for dataforge.strings — passes once the stubs are implemented."""

import pytest

from dataforge import strings


def test_normalize_whitespace_collapses_and_strips():
    assert strings.normalize_whitespace("  hello   world \n") == "hello world"


def test_normalize_whitespace_leaves_single_words():
    assert strings.normalize_whitespace("nospace") == "nospace"


def test_normalize_whitespace_all_whitespace_becomes_empty():
    assert strings.normalize_whitespace("   \t\n ") == ""


def test_slugify_basic():
    assert strings.slugify("  Hello,  World! ") == "hello-world"


def test_slugify_drops_symbols():
    assert strings.slugify("Rock & Roll") == "rock-roll"


def test_slugify_idempotent_on_a_slug():
    assert strings.slugify("already-a-slug") == "already-a-slug"


def test_truncate_returns_short_text_unchanged():
    assert strings.truncate("hi", 8) == "hi"


def test_truncate_shortens_long_text():
    assert strings.truncate("hello world", 8) == "hello..."


def test_truncate_honors_custom_suffix():
    assert strings.truncate("hello world", 7, suffix="~") == "hello ~"


def test_truncate_rejects_suffix_longer_than_limit():
    with pytest.raises(ValueError):
        strings.truncate("hello world", 2)


def test_word_count():
    assert strings.word_count("  one  two three ") == 3


def test_word_count_empty():
    assert strings.word_count("") == 0
