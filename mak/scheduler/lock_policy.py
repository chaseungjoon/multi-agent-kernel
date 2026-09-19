"""Which lock resources a task needs, in which modes (Wave 20).

One function answers this for everyone who needs to know: the scheduler, which
acquires the set atomically before dispatch; the session, which re-validates at
commit that the task still holds what it was granted; and the deadlock watchdog,
which must describe waits the way the scheduler makes them. When those three
built their lists separately, the watchdog described every wait as a plain
WRITE on each target no matter what the scheduler had asked for.

With every flag off the result is exactly the pre-Wave-20 set — WRITE on each
target, READ on each context node — so the policy is also the ablation switch
the scaling study needs. The flags add:

- **api_locks (P2)** — ``X#api`` guards X's interface, ``X`` its body. A task
  read-locks ``callee#api`` for each node its targets call, and write-locks
  ``target#api`` unless it declared a body-only edit (``changes_api=False``).
  An undeclared task (``None``) is treated as possibly changing every target's
  interface — the conservative reading of silence.
- **intention_locks (P6)** — a fragment write holds INTENT_WRITE on its bare
  file id, and a method (or class-body) write on its class node. A whole-file
  or whole-class writer asks for WRITE there and therefore cannot run beside
  any writer below it; writers below it still run beside each other.
- **registry_keys (P5)** — a target the session judges *commutative* (a keyed
  registrar) is held INTENT_WRITE, which appenders co-hold, and each declared
  key is WRITE-locked as ``X#key=<k>``. The commit merges the append onto
  whatever the table holds by then.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from mak.core.types import LockMode, NodeId, SubTask
from mak.lock_manager.resources import api_resource, key_resource

_RANK = {LockMode.READ: 1, LockMode.INTENT_WRITE: 2, LockMode.WRITE: 3}


def _no_callees(node_id: NodeId) -> frozenset[NodeId]:
    return frozenset()


def _no_fragments(node_id: NodeId) -> list[NodeId]:
    return []


def _never_commutative(task: SubTask, node_id: NodeId) -> bool:
    return False


@dataclass(frozen=True)
class LockPolicy:
    """The lock-granularity switches plus the lookups they need.

    The lookups are injected so the policy stays a pure function of the task:
    ``callees`` comes from the pre-wave ``DepGraph``, ``fragments_of`` expands a
    whole-file target into its stored fragments, and ``commutative`` decides
    whether a target is a keyed registrar the task may append to concurrently.
    """

    api_locks: bool = False
    intention_locks: bool = False
    registry_keys: bool = False
    callees: Callable[[NodeId], frozenset[NodeId]] = field(default=_no_callees)
    fragments_of: Callable[[NodeId], list[NodeId]] = field(default=_no_fragments)
    commutative: Callable[[SubTask, NodeId], bool] = field(
        default=_never_commutative
    )


LEGACY_POLICY = LockPolicy()


def lock_requests(
    task: SubTask, policy: LockPolicy = LEGACY_POLICY
) -> list[tuple[NodeId, LockMode]]:
    """Return every ``(resource, mode)`` the task must hold, strongest mode wins.

    A resource is requested once: when two rules ask for it, the stronger mode
    is kept (WRITE > INTENT_WRITE > READ), so a node that is both a target and a
    context node is requested WRITE only. Order is first-request order, which
    keeps the legacy list byte-identical when every flag is off.
    """
    wanted: dict[NodeId, LockMode] = {}

    def want(resource: NodeId, mode: LockMode) -> None:
        held = wanted.get(resource)
        if held is None or _RANK[mode] > _RANK[held]:
            wanted[resource] = mode

    targets = list(dict.fromkeys(task.target_nodes))
    for target in targets:
        _request_target(task, target, policy, want)
    for node_id in task.context_nodes:
        want(node_id, LockMode.READ)
    if policy.intention_locks:
        for target in targets:
            for parent in intention_parents(target):
                want(parent, LockMode.INTENT_WRITE)
    if policy.api_locks:
        _request_api(task, targets, policy, want)
    return list(wanted.items())


def _request_target(
    task: SubTask,
    target: NodeId,
    policy: LockPolicy,
    want: Callable[[NodeId, LockMode], None],
) -> None:
    """WRITE the target, or co-holdable INTENT_WRITE + key locks for a registrar."""
    if policy.registry_keys and policy.commutative(task, target):
        want(target, LockMode.INTENT_WRITE)
        for key in task.registry_keys.get(target, []):
            want(key_resource(target, key), LockMode.WRITE)
        return
    want(target, LockMode.WRITE)


def _request_api(
    task: SubTask,
    targets: list[NodeId],
    policy: LockPolicy,
    want: Callable[[NodeId, LockMode], None],
) -> None:
    """Interface locks: WRITE what the task may change, READ what it calls."""
    for target in api_write_targets(task):
        for node_id in (target, *policy.fragments_of(target)):
            want(api_resource(node_id), LockMode.WRITE)
    owned = set(targets)
    for target in targets:
        for callee in sorted(policy.callees(target)):
            if callee not in owned:
                want(api_resource(callee), LockMode.READ)


def api_write_targets(task: SubTask) -> list[NodeId]:
    """Return the targets whose interface the task may change.

    ``changes_api=None`` (undeclared) → every target; ``False`` → none; ``True``
    → ``api_targets`` plus any contracted node, or every target when neither
    narrows it.
    """
    if task.changes_api is False:
        return []
    targets = list(dict.fromkeys(task.target_nodes))
    if task.changes_api is None:
        return targets
    narrowed = list(dict.fromkeys([*task.api_targets, *task.contract]))
    return narrowed or targets


def intention_parents(target: NodeId) -> list[NodeId]:
    """Return the coarser resources a fragment write holds INTENT_WRITE on.

    ``pkg/m.py::method::C.get`` → ``pkg/m.py`` and ``pkg/m.py::class::C``;
    ``pkg/m.py::function::f`` → ``pkg/m.py``; a bare whole-file id → nothing
    (it *is* the coarsest resource).
    """
    text = str(target)
    if "::" not in text:
        return []
    file_path, kind, name = (text.split("::", 2) + ["", ""])[:3]
    parents = [NodeId(file_path)]
    if kind in ("method", "class_body") and name:
        owner = name.split(".", 1)[0].split("#", 1)[0]
        if owner:
            parents.append(NodeId(f"{file_path}::class::{owner}"))
    return parents
