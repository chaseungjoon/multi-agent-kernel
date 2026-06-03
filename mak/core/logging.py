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
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"


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
        flushed, so events never interleave or truncate (risk L5).
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
