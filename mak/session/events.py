"""The session's event sink and the phase timer built on it."""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from mak.core.logging import EventType, SessionLogger

_P = ParamSpec("_P")
_R = TypeVar("_R")


class EventLog:
    """Log an event stamped with the session id; a no-op without a logger.

    Every collaborator logs through the one instance its session built, so every
    event in a run carries the same ``session_id`` whichever object emitted it.
    """

    def __init__(self, logger: SessionLogger | None, session_id: str) -> None:
        self._logger = logger
        self._session_id = session_id

    def __call__(self, event: EventType, **payload: object) -> None:
        """Append ``event`` with ``payload`` to the session log, if there is one."""
        if self._logger is not None:
            self._logger.log(event, session_id=self._session_id, **payload)


def timed_phase(
    phase: str,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Log the wall duration of a method as a ``PHASE_SPAN``, however it exits.

    The owner (the method's ``self``) logs the span through its ``_log`` attribute,
    which every collaborator that times a phase holds as an :class:`EventLog`.
    """

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(function)
        def measured(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            started = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                owner = args[0] if args else None
                logger = getattr(owner, "_log", None)
                if callable(logger):
                    logger(
                        EventType.PHASE_SPAN,
                        phase=phase,
                        duration_seconds=time.perf_counter() - started,
                    )

        return measured

    return decorate
