"""Structured session logger for MAK event streams."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class EventType(StrEnum):
    """Categories of observable session events."""

    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    LOCK_ACQUIRED = "lock_acquired"
    LOCK_RELEASED = "lock_released"
    CONFLICT_DETECTED = "conflict_detected"
    AGENT_SPAWNED = "agent_spawned"
    # What context a bundle actually carried, per attempt: how many write/read
    # sources and API digests, and their total size. A bundle's context is
    # everything the agent knows about the codebase, and MAK used to dispatch an
    # *empty* one — for a task whose targets are new files, every enrichment layer
    # came back with nothing — without recording it anywhere. The only report came
    # from the one agent that refused to guess; the rest guessed and were counted
    # as completed. ``starved: true`` marks a bundle the kernel refused to send.
    TASK_DISPATCHED = "task_dispatched"
    # What an agent actually returned, per attempt: success, the node ids, the
    # length of each returned source, and any error. Without it a run that drops
    # an agent's work cannot be diagnosed after the fact — the log recorded only
    # that the task made no progress, never what came back over the wire.
    AGENT_RESULT = "agent_result"
    # A returned source MAK refused to stage, with the id and the task's grant.
    # Silent data loss in the agent -> store transport is never acceptable.
    SOURCE_DROPPED = "source_dropped"
    # A task closed because the agent *asserted* there was nothing to change,
    # not because it produced work. Logged separately from TASK_COMPLETED: an
    # operator reading "4 completed" otherwise has no way to learn which of them
    # changed a line, and "decided there was none" is a different claim from
    # "did the work".
    ACCEPTED_NOOP = "accepted_noop"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    PLAN_VALIDATED = "plan_validated"
    PLAN_METRICS = "plan_metrics"


@dataclass(frozen=True, slots=True)
class LogEntry:
    """Single structured event in a session log."""

    timestamp: float
    event_type: EventType
    payload: dict[str, object]

    def to_json(self) -> str:
        """Serialize to a JSON string."""
        return json.dumps(
            {
                "timestamp": self.timestamp,
                "event_type": self.event_type.value,
                "payload": self.payload,
            }
        )

    @classmethod
    def from_json(cls, line: str) -> LogEntry:
        """Deserialize from a JSON string."""
        data = json.loads(line)
        return cls(
            timestamp=data["timestamp"],
            event_type=EventType(data["event_type"]),
            payload=data["payload"],
        )


class SessionLogger:
    """Append-only JSON Lines logger for session events."""

    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event_type: EventType, **payload: object) -> None:
        """Append a timestamped event to the log file.

        Thread-safe: concurrent agents serialize on a lock and each line is
        flushed, so events never interleave or truncate.
        """
        entry = LogEntry(
            timestamp=time.time(),
            event_type=event_type,
            payload=payload,
        )
        line = entry.to_json() + "\n"
        with self._lock, self._path.open("a") as f:
            f.write(line)
            f.flush()

    def read_log(self) -> list[LogEntry]:
        """Read all entries from the log file."""
        if not self._path.exists():
            return []
        text = self._path.read_text()
        if not text.strip():
            return []
        return [LogEntry.from_json(line) for line in text.strip().splitlines()]

    def clear(self) -> None:
        """Truncate the log file."""
        self._path.write_text("")
