"""Wait graph construction and DFS cycle detection for deadlock resolution."""

from __future__ import annotations

from mak.core.types import LockMode, NodeId


WaitQueue = list[tuple[str, NodeId, LockMode]]


class DeadlockDetector:
    """Build a wait graph and detect deadlock cycles via DFS."""

    def build_wait_graph(
        self,
        held: dict[NodeId, list[tuple[str, LockMode]]],
        waiting: WaitQueue,
    ) -> dict[str, set[str]]:
        """Build a directed graph: waiter -> set of holders blocking it."""
        graph: dict[str, set[str]] = {}
        for waiter, node_id, wait_mode in waiting:
            if waiter not in graph:
                graph[waiter] = set()
            holders = held.get(node_id, [])
            for holder, held_mode in holders:
                if holder == waiter:
                    continue
                if self._conflicts(wait_mode, held_mode):
                    graph[waiter].add(holder)
        return graph

    @staticmethod
    def _conflicts(requested: LockMode, held: LockMode) -> bool:
        if requested == LockMode.READ:
            return held == LockMode.WRITE
        if requested == LockMode.WRITE:
            return True
        if requested == LockMode.INTENT_WRITE:
            return held == LockMode.WRITE
        return False  # pragma: no cover

    def find_cycles(self, graph: dict[str, set[str]]) -> list[list[str]]:
        """Find all cycles in the wait graph using DFS."""
        visited: set[str] = set()
        on_stack: set[str] = set()
        path: list[str] = []
        cycles: list[list[str]] = []

        def dfs(node: str) -> None:
            visited.add(node)
            on_stack.add(node)
            path.append(node)

            for neighbor in graph.get(node, set()):
                if neighbor not in visited:
                    dfs(neighbor)
                elif neighbor in on_stack:
                    idx = path.index(neighbor)
                    cycle = path[idx:]
                    cycles.append(cycle)

            path.pop()
            on_stack.discard(node)

        all_nodes: set[str] = set(graph.keys())
        for targets in graph.values():
            all_nodes.update(targets)

        for node in all_nodes:
            if node not in visited:
                dfs(node)

        return cycles

    def resolve(
        self,
        cycle: list[str],
        task_start_times: dict[str, float],
    ) -> str:
        """Wound-wait resolution: abort the youngest task in the cycle."""
        youngest = max(cycle, key=lambda t: task_start_times.get(t, 0.0))
        return youngest
