"""Domain-specific exceptions for MAK."""

from __future__ import annotations


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


class PlanReviewAborted(MakError):
    """Raised when a user aborts the human-in-the-loop plan review."""


class SessionError(MakError):
    """Raised when the session lifecycle cannot proceed."""


class AgentError(MakError):
    """Raised when agent execution fails."""


class UnknownAgentTypeError(AgentError):
    """Raised when an unregistered agent type is requested."""


class AgentResponseError(AgentError):
    """Raised when a provider replied but MAK cannot accept the reply.

    Distinct from a transport failure: the HTTP call succeeded and the model
    said *something*: it was cut off, refused, or came back in a shape the
    protocol cannot decode. Carries the provider's own signals (``stop_reason``,
    ``usage``) so the runner can put them on the ``TaskResult`` and the session
    log can show *why* an attempt produced nothing.

    ``retryable`` is False for a failure that repeats verbatim on an identical
    request (a refusal), so the session stops instead of burning its attempts.

    ``kind`` is the stable slug the session matches on to decide *how* to retry.
    A retry is only worth an attempt if it can differ from the one that failed,
    and what makes it differ depends on which of these went wrong — a truncation
    needs a smaller answer, a schema slip needs the schema restated. Matching on
    the message text instead would tie that decision to prose.
    """

    retryable = True
    kind = "response"

    def __init__(
        self,
        message: str,
        *,
        stop_reason: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason
        self.usage = dict(usage or {})


class AgentTruncatedError(AgentResponseError):
    """Raised when a reply hit the provider's output-token cap mid-generation.

    A truncated structured reply is byte-identical to a deliberate "nothing to
    change" by the time the session sees it, so it must fail here — at the only
    point where the provider's stop signal is still in hand.
    """

    kind = "truncated"


class AgentRefusedError(AgentResponseError):
    """Raised when the model declined to answer. Not retryable."""

    retryable = False
    kind = "refused"


class AgentProtocolError(AgentResponseError):
    """Raised when an agent payload cannot be decoded into a ``TaskResult``.

    A decode failure is not a transport failure: reporting it as "api call
    failed" blames the network for a malformed response body, and a bare
    ``TypeError``/``KeyError`` gives the retry nothing to act on.
    """

    kind = "protocol"


class ConfigError(MakError):
    """Raised when configuration loading or validation fails."""
