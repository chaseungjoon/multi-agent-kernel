"""Tests for mak.conflict_detector.name_collision_check."""

from __future__ import annotations

from mak.conflict_detector.name_collision_check import (
    check_name_collisions,
    extract_defined_symbols,
)


class TestExtractDefinedSymbols:
    def test_top_level_function_and_class(self) -> None:
        src = "def f(): pass\nclass C: pass\n"
        names = {s.qualified_name for s in extract_defined_symbols(src)}
        assert names == {"f", "C"}

    def test_methods_qualified(self) -> None:
        src = "class C:\n    def m(self): pass\n    def n(self): pass\n"
        names = {s.qualified_name for s in extract_defined_symbols(src)}
        assert names == {"C", "C.m", "C.n"}

    def test_async_function(self) -> None:
        symbols = extract_defined_symbols("async def f(): pass")
        assert {s.qualified_name for s in symbols} == {"f"}


class TestCheckNameCollisions:
    def test_distinct_names_no_collision(self) -> None:
        edits = {
            "agent_a": "def alpha(): pass",
            "agent_b": "def beta(): pass",
        }
        assert check_name_collisions(edits) == []

    def test_same_function_two_agents(self) -> None:
        edits = {
            "agent_a": "def handler(): pass",
            "agent_b": "def handler(): pass",
        }
        reasons = check_name_collisions(edits)
        assert len(reasons) == 1
        assert "handler" in reasons[0]
        assert "agent_a" in reasons[0] and "agent_b" in reasons[0]

    def test_same_method_qualified_collision(self) -> None:
        edits = {
            "agent_a": "class C:\n    def run(self): pass\n",
            "agent_b": "class C:\n    def run(self): pass\n",
        }
        reasons = check_name_collisions(edits)
        # Both the class shell 'C' and the method 'C.run' collide.
        joined = " ".join(reasons)
        assert "C.run" in joined

    def test_single_agent_no_collision(self) -> None:
        edits = {"agent_a": "def a(): pass\ndef b(): pass\nclass C: pass\n"}
        assert check_name_collisions(edits) == []

    def test_three_way_collision_lists_all_agents(self) -> None:
        edits = {
            "a1": "def f(): pass",
            "a2": "def f(): pass",
            "a3": "def f(): pass",
        }
        reasons = check_name_collisions(edits)
        assert len(reasons) == 1
        for agent in ("a1", "a2", "a3"):
            assert agent in reasons[0]
