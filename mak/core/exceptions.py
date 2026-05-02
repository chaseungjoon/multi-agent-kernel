"""Domain-specific exceptions for MAK."""


class MakError(Exception):
    """Base exception for MAK domain errors."""


class LockError(MakError):
    """Raised when lock acquisition, release, or validation fails."""


class SchedulingError(MakError):
    """Raised when task scheduling cannot proceed."""


class ConflictDetectionError(MakError):
    """Raised when conflict analysis cannot be completed."""


class GitIntegrationError(MakError):
    """Raised when Git audit-log integration fails."""
