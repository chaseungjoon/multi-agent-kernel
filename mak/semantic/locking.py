"""Build the lock policy a wave runs under from config, store and code graph."""

from __future__ import annotations

from collections import Counter

from mak.config import SemanticConfig
from mak.core.exceptions import NodeStoreError
from mak.core.types import NodeId, SubTask
from mak.node_store.registrar import RegistrarKind, classify
from mak.node_store.store import NodeStore
from mak.planner.depgraph import DepGraph
from mak.scheduler.lock_policy import LockPolicy


def build_lock_policy(
    config: SemanticConfig,
    store: NodeStore,
    graph: DepGraph | None,
    plan: list[SubTask],
) -> LockPolicy:
    """Return the policy for ``plan``, with lookups bound to this wave's state.

    ``graph`` is the pre-wave reference graph; ``None`` disables callee
    interface locks (there is nothing to read them from). Registrar kinds are
    classified once, here, from the committed sources the wave starts from.
    """
    contended = Counter(
        node for task in plan for node in dict.fromkeys(task.target_nodes)
    )
    kinds = registrar_kinds(store, set(contended)) if config.registry_keys else {}

    def commutative(task: SubTask, node_id: NodeId) -> bool:
        kind = kinds.get(node_id)
        if kind is RegistrarKind.KEYED:
            return True
        if kind is RegistrarKind.EMPTY:
            # A stub table carries no evidence of its own; it is treated as a
            # shared table only when the plan contends on it or the task says
            # it will append keys to it.
            return contended[node_id] > 1 or node_id in task.registry_keys
        return False

    def callees(node_id: NodeId) -> frozenset[NodeId]:
        return callees_of(graph, store, node_id)

    def fragments_of(node_id: NodeId) -> list[NodeId]:
        if "::" in str(node_id):
            return []
        return [n for n in store.list_nodes(str(node_id)) if n != node_id]

    return LockPolicy(
        api_locks=config.api_locks,
        intention_locks=config.intention_locks,
        registry_keys=config.registry_keys,
        callees=callees if graph is not None else LockPolicy().callees,
        fragments_of=fragments_of,
        commutative=commutative,
    )


def callees_of(
    graph: DepGraph | None, store: NodeStore, node_id: NodeId
) -> frozenset[NodeId]:
    """Nodes ``node_id`` references, expanding a whole-file id stored as fragments."""
    if graph is None:
        return frozenset()
    direct = graph.references.get(node_id)
    if direct is not None or "::" in str(node_id):
        return direct or frozenset()
    found: set[NodeId] = set()
    fragments = set(store.list_nodes(str(node_id)))
    for fragment in fragments:
        found |= graph.references.get(fragment, frozenset())
    return frozenset(found - fragments)


def registrar_kinds(
    store: NodeStore, nodes: set[NodeId]
) -> dict[NodeId, RegistrarKind]:
    """Classify each of ``nodes`` that is a registrar function."""
    kinds: dict[NodeId, RegistrarKind] = {}
    for node_id in nodes:
        source = _committed_source(store, node_id)
        if source is None:
            continue
        kind = classify(source)
        if kind is not None:
            kinds[node_id] = kind
    return kinds


def _committed_source(store: NodeStore, node_id: NodeId) -> str | None:
    try:
        return store.get_node(node_id).source
    except NodeStoreError:
        return None
