r"""The single canonical lock-mode conflict matrix.

Both ``RWLock`` (grant decisions) and ``DeadlockDetector`` (wait-graph edges)
consume this function, so the two never disagree about what conflicts. The
relation is *directional*, not symmetric: a ``WRITE`` request is blocked by a
held ``INTENT_WRITE`` (a writer cannot proceed while another agent has declared
intent to write), but an ``INTENT_WRITE`` request is not blocked by a held
``INTENT_WRITE`` (multiple agents may co-hold intent — that is what lets the
deadlock detector observe the cycle before it deadlocks).

| requested \\ held | READ | WRITE | INTENT_WRITE |
|-------------------|------|-------|--------------|
| READ              |  ok  | block |     ok       |
| WRITE             | block| block |   block      |
| INTENT_WRITE      |  ok  | block |     ok       |
"""

from __future__ import annotations

from typing import assert_never

from mak.core.types import LockMode


def conflicts(requested: LockMode, held: LockMode) -> bool:
    """Return True if `requested` cannot be granted while another holder has `held`."""
    if requested is LockMode.READ:
        return held is LockMode.WRITE
    if requested is LockMode.WRITE:
        return True  # exclusive: blocked by read, write, and intent_write alike
    if requested is LockMode.INTENT_WRITE:
        return held is LockMode.WRITE
    assert_never(requested)
