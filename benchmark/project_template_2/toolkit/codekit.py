"""codekit operations (unimplemented stubs)."""

from __future__ import annotations


def caesar(s: str, k: int) -> str:
    """Caesar-shift the letters of s forward by k (wrapping within case)."""
    raise NotImplementedError


def run_length_encode(s: str) -> str:
    """Run-length encode a string, e.g. "aaabb" -> "a3b2"."""
    raise NotImplementedError


def run_length_decode(s: str) -> str:
    """Decode a run-length string, e.g. "a3b2" -> "aaabb"."""
    raise NotImplementedError


def to_binary(n: int) -> str:
    """Binary string for non-negative n (no prefix)."""
    raise NotImplementedError


def from_binary(s: str) -> int:
    """Parse a binary digit string into an integer."""
    raise NotImplementedError


def xor_encode(s: str, key: int) -> list:
    """XOR each character code of s with key, returning the list of codes."""
    raise NotImplementedError


def checksum(s: str) -> int:
    """Sum of character codes of s modulo 256."""
    raise NotImplementedError


def hex_encode(s: str) -> str:
    """Encode each character of s as two lower-case hex digits."""
    raise NotImplementedError


def hex_decode(h: str) -> str:
    """Decode a hex-digit string back into characters."""
    raise NotImplementedError


def atbash(s: str) -> str:
    """Apply the Atbash cipher (mirror each letter within its alphabet)."""
    raise NotImplementedError
