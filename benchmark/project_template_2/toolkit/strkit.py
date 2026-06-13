"""strkit operations (unimplemented stubs)."""

from __future__ import annotations


def camel_to_snake(s: str) -> str:
    """Convert camelCase to snake_case, e.g. "fooBarBaz" -> "foo_bar_baz"."""
    raise NotImplementedError


def snake_to_camel(s: str) -> str:
    """Convert snake_case to camelCase, e.g. "foo_bar_baz" -> "fooBarBaz"."""
    raise NotImplementedError


def truncate(s: str, n: int) -> str:
    """Truncate s to at most n chars, using a trailing "..." when shortened (n>=3)."""
    raise NotImplementedError


def word_wrap(s: str, width: int) -> list:
    """Greedily wrap whitespace-separated words into lines of at most width chars."""
    raise NotImplementedError


def levenshtein(a: str, b: str) -> int:
    """Return the Levenshtein edit distance between strings a and b."""
    raise NotImplementedError


def longest_common_prefix(strs: list) -> str:
    """Return the longest common leading prefix of a list of strings."""
    raise NotImplementedError


def is_anagram(a: str, b: str) -> bool:
    """Whether a and b are anagrams, ignoring case and non-alphanumeric characters."""
    raise NotImplementedError


def title_case(s: str) -> str:
    """Capitalise the first letter of each word, e.g. "hello WORLD" -> "Hello World"."""
    raise NotImplementedError


def count_substring(s: str, sub: str) -> int:
    """Count non-overlapping occurrences of sub in s (0 if sub is empty)."""
    raise NotImplementedError


def rot13(s: str) -> str:
    """Apply the ROT13 substitution cipher to the letters of s."""
    raise NotImplementedError
