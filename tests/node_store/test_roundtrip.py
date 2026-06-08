"""Round-trip property tests for the node store.

The load-bearing invariant: ``ingest(file) -> reconstruct`` yields source that is
*semantically equivalent* to the original, with decorators, statement ordering,
and comments intact. These tests are the regression guard for risks C1 (stripped
decorators), C2 (reordered statements), C3 (dropped comments), and C5 (duplicate
node-id collisions).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from mak.node_store.ingestion import parse_file_into_fragments
from mak.node_store.reconstruction import reconstruct_file
from mak.node_store.store import NodeStore


def _normalize(source: str) -> str:
    """Structural fingerprint: AST dump without line/col attributes."""
    return ast.dump(ast.parse(source))


def _comments(source: str) -> list[str]:
    return [
        line.strip() for line in source.splitlines() if line.strip().startswith("#")
    ]


CORPUS: dict[str, str] = {
    "decorated_functions": textwrap.dedent('''\
        """Module docstring."""
        import functools


        @functools.cache
        def cached(x: int) -> int:
            return x * 2


        @staticmethod
        def standalone() -> None:
            pass
    '''),
    "decorated_class_and_methods": textwrap.dedent('''\
        import dataclasses


        @dataclasses.dataclass
        class Point:
            """A point."""

            x: int
            y: int

            @property
            def magnitude(self) -> float:
                return (self.x**2 + self.y**2) ** 0.5

            @staticmethod
            def origin() -> "Point":
                return Point(0, 0)
    '''),
    "order_dependent": textwrap.dedent('''\
        BASE = 10


        def zebra() -> int:
            return BASE


        def apple() -> int:
            return zebra()


        RESULT = apple
    '''),
    "comments_everywhere": textwrap.dedent('''\
        # leading module comment
        import os  # inline on import


        # standalone comment before greet
        def greet(name: str) -> str:
            # body comment
            return f"hi {name}"  # trailing comment


        # comment between functions
        def bye() -> str:
            return "bye"
    '''),
    "top_level_code_between_defs": textwrap.dedent('''\
        import logging

        log = logging.getLogger(__name__)


        def setup() -> None:
            log.info("setup")


        log.info("module imported")


        def teardown() -> None:
            log.info("teardown")
    '''),
    "overloads": textwrap.dedent('''\
        from typing import overload


        @overload
        def f(x: int) -> int: ...
        @overload
        def f(x: str) -> str: ...
        def f(x):
            return x
    '''),
}


class TestRoundTripCorpus:
    @pytest.mark.parametrize("name", sorted(CORPUS))
    def test_semantic_equivalence_via_fragments(self, name: str) -> None:
        original = CORPUS[name]
        frags = parse_file_into_fragments(f"{name}.py", original)
        rebuilt = reconstruct_file(frags, use_ruff=False)
        assert _normalize(rebuilt) == _normalize(original), name

    @pytest.mark.parametrize("name", sorted(CORPUS))
    def test_equivalence_through_store(self, name: str, tmp_path: Path) -> None:
        original = CORPUS[name]
        store = NodeStore(tmp_path / name)
        store.parse_file_into_nodes(f"{name}.py", original)
        frags = store.get_committed_fragments(f"{name}.py")
        rebuilt = reconstruct_file(frags, use_ruff=False)
        assert _normalize(rebuilt) == _normalize(original), name

    @pytest.mark.parametrize("name", sorted(CORPUS))
    def test_comments_preserved(self, name: str) -> None:
        original = CORPUS[name]
        frags = parse_file_into_fragments(f"{name}.py", original)
        rebuilt = reconstruct_file(frags, use_ruff=False)
        for comment in _comments(original):
            assert comment in rebuilt, f"{name}: lost comment {comment!r}"


class TestDecoratorPreservation:
    def test_decorator_kept_in_fragment(self) -> None:
        source = "@staticmethod\ndef f() -> None:\n    pass\n"
        frags = parse_file_into_fragments("d.py", source)
        func = next(f for f in frags if f.kind == "function")
        assert "@staticmethod" in func.source

    def test_property_survives_round_trip(self) -> None:
        source = CORPUS["decorated_class_and_methods"]
        frags = parse_file_into_fragments("c.py", source)
        rebuilt = reconstruct_file(frags, use_ruff=False)
        assert "@property" in rebuilt
        assert "@dataclasses.dataclass" in rebuilt


class TestOrderPreservation:
    def test_zebra_stays_before_apple(self) -> None:
        source = CORPUS["order_dependent"]
        frags = parse_file_into_fragments("o.py", source)
        rebuilt = reconstruct_file(frags, use_ruff=False)
        assert rebuilt.index("def zebra") < rebuilt.index("def apple")


class TestMethodNodes:
    def test_methods_are_separately_addressable(self) -> None:
        source = CORPUS["decorated_class_and_methods"]
        frags = parse_file_into_fragments("c.py", source)
        method_ids = {str(f.node_id) for f in frags if f.kind == "method"}
        assert "c.py::method::Point.magnitude" in method_ids
        assert "c.py::method::Point.origin" in method_ids

    def test_class_shell_present(self) -> None:
        source = CORPUS["decorated_class_and_methods"]
        frags = parse_file_into_fragments("c.py", source)
        shells = [f for f in frags if f.kind == "class"]
        assert len(shells) == 1
        assert "class Point" in shells[0].source


class TestDuplicateIds:
    def test_overload_ids_do_not_collide(self) -> None:
        source = CORPUS["overloads"]
        frags = parse_file_into_fragments("ov.py", source)
        func_ids = [str(f.node_id) for f in frags if f.kind == "function"]
        assert len(func_ids) == len(set(func_ids)), "duplicate node ids collided"
        assert len(func_ids) == 3  # two overloads + the implementation

    def test_store_keeps_all_overloads(self, tmp_path: Path) -> None:
        store = NodeStore(tmp_path / "ov")
        ids = store.parse_file_into_nodes("ov.py", CORPUS["overloads"])
        funcs = [i for i in ids if "::function::" in str(i)]
        assert len(funcs) == 3


def test_mak_can_ingest_its_own_source() -> None:
    """MAK must round-trip its own decorated, ordered, commented source."""
    repo_root = Path(__file__).resolve().parents[2]
    targets = [
        repo_root / "mak" / "lock_manager" / "rwlock.py",
        repo_root / "mak" / "config.py",
        repo_root / "mak" / "node_store" / "ingestion.py",
    ]
    for target in targets:
        original = target.read_text(encoding="utf-8")
        frags = parse_file_into_fragments(str(target), original)
        rebuilt = reconstruct_file(frags, use_ruff=False)
        assert _normalize(rebuilt) == _normalize(original), target.name


def test_unique_names_get_clean_ids() -> None:
    # Disambiguation only kicks in on collision; unique names stay unsuffixed.
    frags = parse_file_into_fragments("u.py", "def a(): ...\ndef b(): ...\n")
    ids = {str(f.node_id) for f in frags}
    assert "u.py::function::a" in ids
    assert "u.py::function::b" in ids
    assert not any("#" in i for i in ids)
