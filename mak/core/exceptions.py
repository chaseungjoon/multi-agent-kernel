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


class NodeStoreError(MakError):
    """Raised when node store operations fail."""


class PlannerFailedError(MakError):
    """Raised when planner exhausts retries."""


class AgentError(MakError):
    """Raised when agent execution fails."""


class UnknownAgentTypeError(AgentError):
    """Raised when an unregistered agent type is requested."""


class ConfigError(MakError):
    """Raised when configuration loading or validation fails."""
