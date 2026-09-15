"""Bound acceptance tests, including unbounded agent loops."""

from collections.abc import Iterator
import signal
from types import FrameType

import pytest


def _timeout(signum: int, frame: FrameType | None) -> None:
    raise TimeoutError("benchmark acceptance test exceeded five seconds")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    """Install and restore a per-test alarm on POSIX hosts."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return
    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
