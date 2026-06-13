"""numkit operations (unimplemented stubs)."""

from __future__ import annotations


def is_perfect_square(n: int) -> bool:
    """Whether the non-negative integer n is a perfect square."""
    raise NotImplementedError


def prime_factors(n: int) -> list:
    """Return the prime factors of n in ascending order, with multiplicity."""
    raise NotImplementedError


def nth_prime(k: int) -> int:
    """Return the k-th prime (k>=1; nth_prime(1) == 2)."""
    raise NotImplementedError


def collatz_steps(n: int) -> int:
    """Number of Collatz steps to reach 1 from n (n>=1)."""
    raise NotImplementedError


def gcd_many(nums: list) -> int:
    """Greatest common divisor of a non-empty list of integers."""
    raise NotImplementedError


def lcm_many(nums: list) -> int:
    """Least common multiple of a non-empty list of integers."""
    raise NotImplementedError


def base_convert(n: int, base: int) -> str:
    """Render non-negative n in the given base (2-36) with lower-case digits."""
    raise NotImplementedError


def clamp(x: int, lo: int, hi: int) -> int:
    """Constrain x to the inclusive range [lo, hi]; raise if lo > hi."""
    raise NotImplementedError


def divisors(n: int) -> list:
    """Return all positive divisors of n in ascending order (n>=1)."""
    raise NotImplementedError


def mean(nums: list) -> float:
    """Arithmetic mean of a non-empty list of numbers."""
    raise NotImplementedError
