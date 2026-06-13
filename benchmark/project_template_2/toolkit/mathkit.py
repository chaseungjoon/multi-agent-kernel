"""mathkit operations (unimplemented stubs)."""

from __future__ import annotations


def fib(n: int) -> int:
    """n-th Fibonacci number (fib(0)=0, fib(1)=1); raise ValueError if n<0."""
    raise NotImplementedError


def factorial(n: int) -> int:
    """n! for non-negative n (0! == 1); raise ValueError if n<0."""
    raise NotImplementedError


def binomial(n: int, k: int) -> int:
    """Binomial coefficient C(n, k); 0 when out of range."""
    raise NotImplementedError


def sum_digits(n: int) -> int:
    """Sum of the decimal digits of abs(n)."""
    raise NotImplementedError


def digital_root(n: int) -> int:
    """Repeated digit sum of abs(n) until a single digit remains."""
    raise NotImplementedError


def is_armstrong(n: int) -> bool:
    """Whether n equals the sum of its digits each raised to the digit count."""
    raise NotImplementedError


def triangular(n: int) -> int:
    """n-th triangular number 0+1+...+n (n>=0)."""
    raise NotImplementedError


def sum_primes_below(n: int) -> int:
    """Sum of all primes strictly below n."""
    raise NotImplementedError


def power_mod(b: int, e: int, m: int) -> int:
    """Modular exponentiation (b**e) % m by fast squaring."""
    raise NotImplementedError


def is_prime(n: int) -> bool:
    """Whether n is a prime number."""
    raise NotImplementedError
