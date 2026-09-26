"""The MAK session: the state machine that drives init → plan → run → teardown.

``Session`` lives in :mod:`mak.session.core`; the collaborators it delegates to
each live in their own module in this package. Everything a caller imports from
``mak.session`` is re-exported here.
"""

from mak.session.core import Session
from mak.session.types import (
    PlanProposal,
    SessionResult,
    SessionState,
    SubTaskProgress,
    TestRunner,
)

__all__ = [
    "PlanProposal",
    "Session",
    "SessionResult",
    "SessionState",
    "SubTaskProgress",
    "TestRunner",
]
