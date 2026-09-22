"""Narrowing helpers for values read back out of JSON.

``json.loads`` returns ``object``. Rather than sprinkle ``cast`` or silence the
type checker, every read of a profile or a ``run_meta`` blob goes through one of
these, which narrow explicitly and fall back to a documented default when the
document does not have the shape the caller expects.
"""

from __future__ import annotations


def as_mapping(value: object) -> dict[str, object]:
    """Narrow to a string-keyed mapping, or an empty one."""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def as_sequence(value: object) -> list[object]:
    """Narrow to a list, or an empty one."""
    return list(value) if isinstance(value, list) else []


def as_int(value: object, default: int = 0) -> int:
    """Narrow to an int, or ``default`` when the value is not numeric."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def as_float(value: object, default: float = 0.0) -> float:
    """Narrow to a float, or ``default`` when the value is not numeric."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def as_text(value: object, default: str = "") -> str:
    """Narrow to a string, or ``default`` when the value is absent."""
    if isinstance(value, str):
        return value
    return default if value is None else str(value)


def field(container: object, key: str) -> object:
    """Read one key out of a value that should be a mapping."""
    return as_mapping(container).get(key)
