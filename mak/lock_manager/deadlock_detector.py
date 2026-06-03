"""Wait graph construction and iterative cycle detection for deadlock resolution."""

from __future__ import annotations

from collections.abc import Iterator

from mak.core.types import LockMode, NodeId
from mak.lock_manager.conflicts import conflicts

WaitQueue = list[tuple[str, NodeId, LockMode]]


class DeadlockDetector:
    """Build a wait graph and detect deadlock cycles via iterative DFS."""

    def build_wait_graph(
        self,
        held: dict[NodeId, list[tuple[str, LockMode]]],
        waiting: WaitQueue,
    ) -> dict[str, set[str]]:
        """Build a directed graph: waiter -> set of holders blocking it."""
        graph: dict[str, set[str]] = {}
        for waiter, node_id, wait_mode in waiting:
            graph.setdefault(waiter, set())
            for holder, held_mode in held.get(node_id, []):
                if holder != waiter and conflicts(wait_mode, held_mode):
                    graph[waiter].add(holder)
        return graph

    @staticmethod
    def _canonical(cycle: list[str]) -> tuple[str, ...]:
        """Rotate a cycle to start at its smallest member so rotations dedupe."""
        pivot = min(range(len(cycle)), key=cycle.__getitem__)
        return tuple(cycle[pivot:] + cycle[:pivot])

    def find_cycles(self, graph: dict[str, set[str]]) -> list[list[str]]:
        """Find all distinct cycles in the wait graph using iterative DFS.

        Iterative (no recursion limit on deep graphs) and deduplicated: each
        directed cycle is reported once regardless of which rotation is found.
        """
        all_nodes = set(graph) | {t for targets in graph.values() for t in targets}
        visited: set[str] = set()
        seen_cycles: set[tuple[str, ...]] = set()
        cycles: list[list[str]] = []

        for root in all_nodes:
            if root in visited:
                continue
            self._walk(root, graph, visited, seen_cycles, cycles)
        return cycles

    def _walk(
        self,
        root: str,
        graph: dict[str, set[str]],
        visited: set[str],
        seen_cycles: set[tuple[str, ...]],
        cycles: list[list[str]],
    ) -> None:
        """Run iterative DFS from `root`, recording any cycles reached."""
        stack: list[tuple[str, Iterator[str]]] = [(root, iter(graph.get(root, set())))]
        path: list[str] = [root]
        on_path: set[str] = {root}

        while stack:
            node, neighbors = stack[-1]
            descended = False
            for neighbor in neighbors:
                if neighbor in on_path:
                    cycle = path[path.index(neighbor):]
                    key = self._canonical(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(list(cycle))
                elif neighbor not in visited:
                    stack.append((neighbor, iter(graph.get(neighbor, set()))))
                    path.append(neighbor)
                    on_path.add(neighbor)
                    descended = True
                    break
            if not descended:
                visited.add(node)
                on_path.discard(node)
                path.pop()
                stack.pop()

    def resolve(
        self,
        cycle: list[str],
        task_start_times: dict[str, float],
    ) -> str:
        """Wound-wait resolution: abort the youngest task in the cycle."""
        return max(cycle, key=lambda t: task_start_times.get(t, 0.0))
