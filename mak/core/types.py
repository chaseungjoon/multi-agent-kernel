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
    """A task sent to an agent for execution."""

    task_id: str
    description: str
    target_nodes: list[NodeId] = field(default_factory=list)
    locks: list[LockEntry] = field(default_factory=list)
    context: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Result of an agent task execution."""

    task_id: str
    success: bool
    modified_nodes: list[NodeId] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SubTask:
    """A decomposed sub-task with dependency tracking.

    ``target_nodes`` are the nodes the task will *write*; ``context_nodes`` are
    nodes it needs to *read* to do the work (sibling methods, class attributes,
    imports). The runner ships the current source of both to the agent so it is
    not editing blind (PLANS.md §8).
    """

    task_id: str
    description: str
    target_nodes: list[NodeId] = field(default_factory=list)
    context_nodes: list[NodeId] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    agent_type: str = ""
