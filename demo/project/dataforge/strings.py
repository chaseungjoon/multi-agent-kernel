"""String helpers.

Each function is an unimplemented stub. Implement it to match its docstring
(``tests/test_strings.py`` is the executable specification).
"""

from __future__ import annotations


def normalize_whitespace(text: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends.

    Examples:
        >>> normalize_whitespace("  hello   world \\n")
        'hello world'
        >>> normalize_whitespace("nospace")
        'nospace'
        >>> normalize_whitespace("   ")
        ''
    """
    raise NotImplementedError


def slugify(text: str) -> str:
    """Return a URL-friendly slug.

    Lowercase the text and normalize its whitespace, drop every character that is
    not a letter, digit, space, or hyphen, replace spaces with hyphens, then
    collapse repeated hyphens and strip any leading/trailing hyphen.

    Examples:
        >>> slugify("  Hello,  World! ")
        'hello-world'
        >>> slugify("Rock & Roll")
        'rock-roll'
        >>> slugify("already-a-slug")
        'already-a-slug'
    """
    raise NotImplementedError


def truncate(text: str, limit: int, suffix: str = "...") -> str:
    """Shorten ``text`` to at most ``limit`` characters.

    If ``text`` already fits within ``limit`` it is returned unchanged. Otherwise
    return the first ``limit - len(suffix)`` characters of ``text`` followed by
    ``suffix`` (so the result is exactly ``limit`` characters long). Raise
    ``ValueError`` if ``suffix`` is longer than ``limit``.

    Examples:
        >>> truncate("hello world", 8)
        'hello...'
        >>> truncate("hi", 8)
        'hi'
        >>> truncate("hello world", 7, suffix="~")
        'hello ~'
    """
    raise NotImplementedError


def word_count(text: str) -> int:
    """Return the number of whitespace-separated words in ``text``.

    Examples:
        >>> word_count("  one  two three ")
        3
        >>> word_count("")
        0
    """
    raise NotImplementedError
