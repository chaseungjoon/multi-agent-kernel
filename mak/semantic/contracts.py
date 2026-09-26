"""Session-side use of declared contracts.

Which dependency edges may become *soft* under ``semantic.contract_dispatch``,
which contracts a task should be shown and checked against, and how a contract
reads to an agent. The contract itself — parsing, normalizing, comparing an
implementation to it — lives in :mod:`mak.planner.contracts`.
"""

from __future__ import annotations

from dataclasses import replace

from mak.core.types import LockMode, NodeId, SubTask
from mak.lock_manager.conflicts import conflicts
from mak.scheduler.lock_policy import LockPolicy, lock_requests

CONTRACT_PREFIX = "contract"


def soft_edges(
    tasks: list[SubTask], policy: LockPolicy
) -> tuple[list[SubTask], dict[str, set[str]]]:
    """Soften the dependency edges a dependent may be dispatched ahead of.

    Returns the plan (rewritten where an edge softened) and the soft edges. An
    edge ``provider -> dependent`` softens only when all three hold:

    - the provider declared a contract for **every** node it writes, so the
      dependent is shown the whole interface it is waiting for;
    - the two write no node in common;
    - their lock sets do not conflict — the dependent will wait, holding its
      locks, for the provider to commit, so a provider that needed one of those
      locks could never start.

    A softened dependent no longer lists the provider's targets as context:
    the contract stands in for them, and what the store holds there is the
    *old* code the provider is replacing — both misleading to show and a READ
    lock that would block the provider it waits for.
    """
    by_id = {t.task_id: t for t in tasks}
    edges: dict[str, set[str]] = {}
    rewritten: dict[str, SubTask] = {}
    for task in tasks:
        current = task
        for dep_id in task.depends_on:
            provider = by_id.get(dep_id)
            if provider is None or not _fully_contracted(provider):
                continue
            if set(provider.target_nodes) & set(current.target_nodes):
                continue
            candidate = replace(
                current,
                context_nodes=[
                    n for n in current.context_nodes
                    if n not in set(provider.target_nodes)
                ],
            )
            if _locks_conflict(
                lock_requests(candidate, policy), lock_requests(provider, policy)
            ):
                continue
            current = candidate
            edges.setdefault(task.task_id, set()).add(dep_id)
        rewritten[task.task_id] = current
    return [rewritten[t.task_id] for t in tasks], edges


def _fully_contracted(task: SubTask) -> bool:
    return bool(task.contract) and set(task.target_nodes) <= set(task.contract)


def _locks_conflict(
    a: list[tuple[NodeId, LockMode]], b: list[tuple[NodeId, LockMode]]
) -> bool:
    held = dict(b)
    for resource, mode in a:
        other = held.get(resource)
        if other is not None and (conflicts(mode, other) or conflicts(other, mode)):
            return True
    return False


def visible_contracts(task: SubTask, plan: dict[str, SubTask]) -> dict[NodeId, str]:
    """Return the contracts a task is built against.

    Its own contracts are the signatures it must implement; a direct
    dependency's are the interface it builds on; any contract on a node the
    planner listed as its context is the interface it was told to read.
    """
    shown: dict[NodeId, str] = dict(task.contract)
    for dep_id in task.depends_on:
        provider = plan.get(dep_id)
        if provider is not None:
            shown.update(provider.contract)
    wanted = set(task.context_nodes)
    for other in plan.values():
        for node, text in other.contract.items():
            if node in wanted:
                shown.setdefault(node, text)
    return shown


def render_contract(node_id: NodeId, text: str, *, own: bool) -> str:
    """Return a self-describing contract entry for the bundle's context."""
    role = (
        "you must implement it with exactly this signature"
        if own
        else "build against exactly this signature; it is fixed by the plan"
    )
    return f"# Declared contract for {node_id} — {role}.\n{text.strip()}\n"
