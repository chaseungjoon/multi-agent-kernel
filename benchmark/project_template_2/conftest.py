"""Project root on sys.path + a per-test timeout.

A real agent can implement one of the algorithmic functions (e.g. a while-based
``collatz_steps`` or ``nth_prime``) with an infinite loop. Without a guard, that test
would hang ``pytest`` forever and wedge the whole benchmark. A small SIGALRM-based hook
aborts any test that runs longer than the limit, so the runaway test simply fails (and
is correctly counted as not-passed) instead of hanging. Applies equally to the MAK and
worktree measurements, so it does not favour either side.
"""

import signal

import pytest

_PER_TEST_TIMEOUT_S = 5


def _on_timeout(signum, frame):
    raise TimeoutError(f"test exceeded {_PER_TEST_TIMEOUT_S}s (likely an infinite loop)")


signal.signal(signal.SIGALRM, _on_timeout)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    signal.setitimer(signal.ITIMER_REAL, _PER_TEST_TIMEOUT_S)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
