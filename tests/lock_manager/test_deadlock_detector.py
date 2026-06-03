"""Tests for mak.lock_manager.deadlock_detector."""

from __future__ import annotations

from mak.core.types import LockMode, NodeId
from mak.lock_manager.deadlock_detector import DeadlockDetector


class TestDeadlockDetector:
    def setup_method(self) -> None:
        self.detector = DeadlockDetector()

    def test_no_cycle(self) -> None:
        graph: dict[str, set[str]] = {
            "agent_a": {"agent_b"},
            "agent_b": set(),
        }
        cycles = self.detector.find_cycles(graph)
        assert cycles == []

    def test_simple_cycle(self) -> None:
        graph: dict[str, set[str]] = {
            "agent_a": {"agent_b"},
            "agent_b": {"agent_a"},
        }
        cycles = self.detector.find_cycles(graph)
        assert len(cycles) >= 1
        flat = {agent for cycle in cycles for agent in cycle}
        assert "agent_a" in flat
        assert "agent_b" in flat

    def test_three_way_cycle(self) -> None:
        graph: dict[str, set[str]] = {
            "agent_a": {"agent_b"},
            "agent_b": {"agent_c"},
            "agent_c": {"agent_a"},
        }
        cycles = self.detector.find_cycles(graph)
        assert len(cycles) >= 1

    def test_build_wait_graph(self) -> None:
        nid = NodeId("mod.py::function::foo")
        held = {nid: [("agent_a", LockMode.WRITE)]}
        waiting = [("agent_b", nid, LockMode.WRITE)]
        graph = self.detector.build_wait_graph(held, waiting)
        assert "agent_b" in graph
        assert "agent_a" in graph["agent_b"]

    def test_read_does_not_conflict_with_read(self) -> None:
        nid = NodeId("mod.py::function::foo")
        held = {nid: [("agent_a", LockMode.READ)]}
        waiting = [("agent_b", nid, LockMode.READ)]
        graph = self.detector.build_wait_graph(held, waiting)
        assert graph.get("agent_b", set()) == set()

    def test_write_conflicts_with_read(self) -> None:
        nid = NodeId("mod.py::function::foo")
        held = {nid: [("agent_a", LockMode.READ)]}
        waiting = [("agent_b", nid, LockMode.WRITE)]
        graph = self.detector.build_wait_graph(held, waiting)
        assert "agent_a" in graph.get("agent_b", set())

    def test_resolve_aborts_youngest(self) -> None:
        cycle = ["agent_a", "agent_b", "agent_c"]
        times = {"agent_a": 1.0, "agent_b": 3.0, "agent_c": 2.0}
        victim = self.detector.resolve(cycle, times)
        assert victim == "agent_b"

    def test_resolve_with_missing_times(self) -> None:
        cycle = ["agent_a", "agent_b"]
        times: dict[str, float] = {"agent_a": 1.0}
        victim = self.detector.resolve(cycle, times)
        assert victim == "agent_a"

    def test_self_waiter_ignored(self) -> None:
        nid = NodeId("mod.py::function::foo")
        held = {nid: [("agent_a", LockMode.WRITE)]}
        waiting = [("agent_a", nid, LockMode.WRITE)]
        graph = self.detector.build_wait_graph(held, waiting)
        assert graph.get("agent_a", set()) == set()

    def test_empty_graph(self) -> None:
        cycles = self.detector.find_cycles({})
        assert cycles == []

    def test_cycle_reported_once(self) -> None:
        # Risk L7: a single directed cycle must not be reported once per rotation.
        graph: dict[str, set[str]] = {
            "agent_a": {"agent_b"},
            "agent_b": {"agent_c"},
            "agent_c": {"agent_a"},
        }
        cycles = self.detector.find_cycles(graph)
        assert len(cycles) == 1
        assert set(cycles[0]) == {"agent_a", "agent_b", "agent_c"}

    def test_deep_chain_no_recursion_error(self) -> None:
        # Iterative DFS must handle graphs deeper than the recursion limit.
        graph: dict[str, set[str]] = {f"a{i}": {f"a{i + 1}"} for i in range(5000)}
        cycles = self.detector.find_cycles(graph)
        assert cycles == []

    def test_two_distinct_cycles(self) -> None:
        graph: dict[str, set[str]] = {
            "a": {"b"},
            "b": {"a"},
            "c": {"d"},
            "d": {"c"},
        }
        cycles = self.detector.find_cycles(graph)
        assert len(cycles) == 2

    def test_write_blocked_by_intent_write_edge(self) -> None:
        # The wait graph now reflects the H2 fix: a writer waits on an intent_writer.
        nid = NodeId("mod.py::function::foo")
        held = {nid: [("agent_a", LockMode.INTENT_WRITE)]}
        waiting = [("agent_b", nid, LockMode.WRITE)]
        graph = self.detector.build_wait_graph(held, waiting)
        assert "agent_a" in graph.get("agent_b", set())
