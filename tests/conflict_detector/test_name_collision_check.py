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

    def test_same_name_in_different_files_is_not_a_collision(self) -> None:
        # One task may legitimately edit the `_register_all` of two *different*
        # registry tables (node-id keys carry the file); that is not a collision.
        edits = {
            "app/routes.py::function::_register_all": (
                "def _register_all() -> None:\n    pass\n"
            ),
            "app/errors.py::function::_register_all": (
                "def _register_all() -> None:\n    pass\n"
            ),
        }
        assert check_name_collisions(edits) == []

    def test_same_name_same_file_node_keys_still_collides(self) -> None:
        # Two different edits to the same file both defining `helper` collide.
        edits = {
            "m.py::function::a": "def a(): pass\ndef helper(): pass\n",
            "m.py::function::b": "def b(): pass\ndef helper(): pass\n",
        }
        reasons = check_name_collisions(edits)
        assert len(reasons) == 1
        assert "helper" in reasons[0]

    def test_two_class_bodies_in_one_file_do_not_collide(self) -> None:
        # Wave 11 audit: class_body fragments are stored dedented, so both parse
        # as a top-level `get`. They belong to different classes and must be
        # attributed as such.
        edits = {
            "m.py::class_body::Registers": "def get(self, name):\n    return name\n",
            "m.py::class_body::Marks#2": "def get(self, name):\n    return name\n",
        }
        assert check_name_collisions(edits) == []

    def test_same_class_body_and_method_still_collide(self) -> None:
        edits = {
            "m.py::class_body::Registers": "def get(self, name):\n    return name\n",
            "m.py::method::Registers.get": "def get(self, name):\n    return 1\n",
        }
        reasons = check_name_collisions(edits)
        assert reasons and "Registers.get" in reasons[0]

    def test_two_whole_file_nodes_do_not_collide(self) -> None:
        # Two freshly created files each defining `main` are two separate scopes,
        # even though a bare-path node id carries no '::'.
        edits = {
            "pkg/a.py": "def main():\n    return 1\n",
            "pkg/b.py": "def main():\n    return 2\n",
        }
        assert check_name_collisions(edits) == []
