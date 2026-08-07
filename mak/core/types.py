"""Shared value objects used across MAK subsystems."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import NewType


class ResourceKind(StrEnum):
    """Kind of codebase resource managed by MAK locks."""

    FILE = "file"
    SYMBOL = "symbol"


class LockMode(StrEnum):
    """Supported reader-writer lock modes."""

    READ = "read"
    WRITE = "write"
    INTENT_WRITE = "intent_write"


NodeId = NewType("NodeId", str)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """Stable reference to a file-level or symbol-level resource."""

    kind: ResourceKind
    path: str
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class NodeFragment:
    """A fragment of an AST node with its source text."""

    node_id: NodeId
    kind: str
    source: str
    version: int


@dataclass(frozen=True, slots=True)
class LockEntry:
    """A held lock on a resource."""

    resource: ResourceRef
    mode: LockMode
    holder: str
    acquired_at: float


@dataclass(frozen=True, slots=True)
class TaskBundle:
    """A task sent to an agent for execution.

    ``retry_note`` is the feedback channel for a re-dispatch: why the previous
    attempt produced nothing, and what to do differently. Without it a retry
    re-issues a byte-identical request, which for a truncated reply yields an
    identically-cut reply — three attempts, three identical failures, full token
    cost. Every adapter surfaces it to the model.
    """

    task_id: str
    description: str
    target_nodes: list[NodeId] = field(default_factory=list)
    locks: list[LockEntry] = field(default_factory=list)
    context: dict[str, str] = field(default_factory=dict)
    retry_note: str | None = None


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Result of an agent task execution.

    ``new_sources`` carries the rewritten source for each node the agent changed
    (``node_id -> full source``). This is how an agent's edit actually reaches the
    node store: the session stages each entry via ``put_node`` before validating
    and committing. ``modified_nodes`` lists the ids that changed (it is derivable
    from ``new_sources`` but kept explicit for the wire and for agents that report
    ids without re-sending source).

    The remaining fields exist because "the agent returned nothing" has several
    causes that used to be indistinguishable:

    - ``no_changes_required`` — the agent's *positive assertion* that it
      inspected the targets and found nothing to change. An empty result without
      it is a failure, not a no-op: a reply cut off at the output-token limit is
      byte-identical to a deliberate "nothing to do", and only one of the two
      may be reported as a completed task.
    - ``stop_reason`` / ``usage`` — the provider's own signals for the attempt,
      carried through so ``AGENT_RESULT`` can show why a reply was empty instead
      of leaving it to be inferred.
    - ``retryable`` — False for a failure that repeats verbatim on an identical
      request (a refusal), so the session fails fast rather than spending its
      whole attempt budget on the same answer.
    - ``error_kind`` — *which* kind of failure it was (``truncated`` /
      ``refused`` / ``protocol`` / ``api``), carried from the exception class so
      the retry can be aimed. "Retryable" only says another attempt is worth
      making; it does not say what to change, and a retry that cannot differ
      from the attempt that failed spends the budget re-earning the same answer.
      One run rejected the same malformed shape three times because the feedback
      never named the schema. Unlike ``retryable``, it is **never read off the
      wire**: it is MAK's classification of why MAK could not use a reply, so an
      agent setting it would be steering the feedback about its own output.
    """

    task_id: str
    success: bool
    modified_nodes: list[NodeId] = field(default_factory=list)
    new_sources: dict[NodeId, str] = field(default_factory=dict)
    error: str | None = None
    no_changes_required: bool = False
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    retryable: bool = True
    error_kind: str | None = None


@dataclass(frozen=True, slots=True)
class SubTask:
    """A decomposed sub-task with dependency tracking.

    ``target_nodes`` are the nodes the task will *write*; ``context_nodes`` are
    nodes it needs to *read* to do the work (sibling methods, class attributes,
    imports). The runner ships the current source of both to the agent so it is
    not editing blind.
    """

    task_id: str
    description: str
    target_nodes: list[NodeId] = field(default_factory=list)
    context_nodes: list[NodeId] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    agent_type: str = ""
