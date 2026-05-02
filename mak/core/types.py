"""Shared value objects used across MAK subsystems."""

from dataclasses import dataclass
from enum import StrEnum


class ResourceKind(StrEnum):
    """Kind of codebase resource managed by MAK locks."""

    FILE = "file"
    SYMBOL = "symbol"


class LockMode(StrEnum):
    """Supported reader-writer lock modes."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Stable reference to a file-level or symbol-level resource."""

    kind: ResourceKind
    path: str
    symbol: str | None = None
