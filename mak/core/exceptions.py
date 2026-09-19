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


class UnsafeNodeIdError(MakError):
    """Raised when a node id would resolve outside the tree it may write to.

    A node id carries a file path, and every id MAK acts on originates with a
    model — the planner names write targets, an agent names what it rewrote. An
    id like ``/etc/cron.d/x.py`` or ``../../.ssh/authorized_keys.py`` is a
    perfectly well-formed Python path that resolves outside the working
    directory, and ``Path(work_dir) / "/etc/x.py"`` collapses to ``/etc/x.py``
    outright. Nothing downstream re-checks it: the store writes fragments by
    joining the id to its root, and reconstruction writes files by joining it to
    the work dir.

    So containment is asserted at every boundary that turns an id into a path,
    and this is what refusing one looks like.
    """


class PlannerFailedError(MakError):
    """Raised when planner exhausts retries."""


class PlanReviewAborted(MakError):
    """Raised when a user aborts the human-in-the-loop plan review."""


class SessionError(MakError):
    """Raised when the session lifecycle cannot proceed."""


class WorkTreeConflictError(MakError):
    """Raised when a file changed underneath MAK and the policy is to stop.

    Between two sessions a human can edit, rename, or delete anything MAK wrote.
    The store cannot tell that from its own fragments — they record what MAK
    last *committed*, not what is on disk now — so the session compares each file
    against the digest of the content MAK last materialized there.

    A difference means someone else's edit. ``session.on_external_edit`` decides
    what happens next: ``"adopt"`` (the default) takes the working tree as the
    newer truth and synchronizes the store to it, while ``"conflict"`` raises
    this — deliberately during startup reconciliation, *before* planning, because
    the failure this prevents is an agent being handed content the tree has not
    held for days and rewriting it back over the human's work.
    """


class ProjectBusyError(MakError):
    """Raised when another live MAK session already owns this project.

    MAK's lock table is an *intra*-process structure: a ``threading.RLock`` makes
    it safe across a session's own worker threads and says nothing whatsoever
    about a second process. Two ``mak`` runs over one project therefore both
    granted write locks on the same node, and each startup cleared the other's
    leases from the persisted table without ever establishing that its owner was
    dead.

    The guarantee is single ownership, not a distributed lock table: one process
    holds an OS-level exclusive lease on the project's ``.mak/`` for as long as
    it runs, and everyone else fails fast with this. The message names the
    holder's pid, host, and how long ago it was last seen, because "busy" without
    a suspect is not something an operator can act on.
    """


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


class AgentContextExceededError(AgentResponseError):
    """Raised when a bundle cannot fit the local model's context window.

    Ollama's runtime context defaults to a few thousand tokens *regardless of
    what the model supports*, and it **silently truncates** an over-long prompt
    rather than erroring. MAK's bundles run to tens of KB, so the naive local
    setup produces a confident wrong answer with nothing in any log to explain
    it — the worst failure mode a code editor can have. The native adapter sizes
    the window itself and, when even the model's real limit cannot hold the
    bundle, refuses here instead.

    Not retryable: the same bundle re-sent is the same overflow, so retrying
    would spend the attempt budget re-earning the identical refusal. The message
    names the estimated prompt size, the model's limit, and the settings that
    fix it, so the session's failure reason is something a user can act on.
    """

    retryable = False
    kind = "context"


class ConfigError(MakError):
    """Raised when configuration loading or validation fails."""


class SemanticGateError(MakError):
    """Raised when an optional semantic gate cannot run (Wave 20).

    A gate is an *extra* check a project opts into — a type checker, the
    impacted tests, an import smoke run, an LLM adjudicator. Its infrastructure
    failing (the tool is not installed, the subprocess timed out) says nothing
    about the wave's code, so the session logs this and carries on rather than
    failing the wave or inventing a defect.
    """


class ContractError(MakError):
    """Raised when a declared API contract cannot be parsed (Wave 20).

    A contract is the planner's promise of a signature (``def f(a: int) -> R``).
    One that does not parse as a Python signature is not a contract anyone can
    be held to, so it is refused where it enters rather than compared later.
    """
