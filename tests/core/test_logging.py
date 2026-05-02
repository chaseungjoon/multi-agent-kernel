"""Tests for structured session logging."""

from pathlib import Path

from mak.core.logging import EventType, LogEntry, SessionLogger


def test_log_and_read_single_event(tmp_path: Path) -> None:
    """Logging an event persists it to disk and can be read back."""
    logger = SessionLogger(tmp_path / "session.jsonl")
    logger.log(EventType.TASK_STARTED, task_id="t1")

    entries = logger.read_log()
    assert len(entries) == 1
    assert entries[0].event_type is EventType.TASK_STARTED
    assert entries[0].payload == {"task_id": "t1"}


def test_events_are_appended_not_overwritten(tmp_path: Path) -> None:
    """Multiple log calls append to the same file."""
    logger = SessionLogger(tmp_path / "session.jsonl")
    logger.log(EventType.SESSION_STARTED)
    logger.log(EventType.SESSION_ENDED)

    entries = logger.read_log()
    assert len(entries) == 2


def test_all_event_type_values_exist() -> None:
    """Every expected event type is present in the enum."""
    expected = {
        "task_started",
        "task_completed",
        "lock_acquired",
        "lock_released",
        "conflict_detected",
        "agent_spawned",
        "session_started",
        "session_ended",
    }
    assert {e.value for e in EventType} == expected


def test_log_entry_serialization_round_trip() -> None:
    """LogEntry survives a to_json / from_json round trip."""
    original = LogEntry(
        timestamp=1234567890.123,
        event_type=EventType.LOCK_ACQUIRED,
        payload={"resource": "mak/core/types.py", "mode": "write"},
    )
    restored = LogEntry.from_json(original.to_json())

    assert restored == original


def test_read_log_preserves_order(tmp_path: Path) -> None:
    """Entries come back in the order they were logged."""
    logger = SessionLogger(tmp_path / "session.jsonl")
    types = [EventType.SESSION_STARTED, EventType.AGENT_SPAWNED, EventType.TASK_STARTED]
    for et in types:
        logger.log(et)

    entries = logger.read_log()
    assert [e.event_type for e in entries] == types


def test_clear_truncates_log(tmp_path: Path) -> None:
    """Clearing the log removes all entries."""
    logger = SessionLogger(tmp_path / "session.jsonl")
    logger.log(EventType.CONFLICT_DETECTED, file="a.py")
    logger.clear()

    assert logger.read_log() == []


def test_log_with_empty_payload(tmp_path: Path) -> None:
    """Events can be logged without any payload kwargs."""
    logger = SessionLogger(tmp_path / "session.jsonl")
    logger.log(EventType.SESSION_STARTED)

    entries = logger.read_log()
    assert entries[0].payload == {}


def test_creates_parent_directories(tmp_path: Path) -> None:
    """SessionLogger creates missing parent directories on init."""
    deep_path = tmp_path / "a" / "b" / "c" / "session.jsonl"
    logger = SessionLogger(deep_path)
    logger.log(EventType.TASK_COMPLETED, result="ok")

    assert deep_path.exists()
    assert len(logger.read_log()) == 1


def test_read_nonexistent_log_returns_empty(tmp_path: Path) -> None:
    """Reading a log file that does not exist returns an empty list."""
    logger = SessionLogger(tmp_path / "missing.jsonl")

    assert logger.read_log() == []


def test_read_empty_log_returns_empty(tmp_path: Path) -> None:
    """Reading an empty log file returns an empty list."""
    log_file = tmp_path / "empty.jsonl"
    log_file.write_text("")
    logger = SessionLogger(log_file)

    assert logger.read_log() == []
