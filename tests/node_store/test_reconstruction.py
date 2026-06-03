"""Tests for mak.node_store.reconstruction."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mak.core.types import NodeFragment, NodeId
from mak.node_store.reconstruction import (
    assemble_fragments,
    format_with_ruff,
    reconstruct_file,
)


def _frag(kind: str, name: str, source: str) -> NodeFragment:
    return NodeFragment(
        node_id=NodeId(f"test.py::{kind}::{name}"),
        kind=kind,
        source=source,
        version=1,
    )


class TestAssembleFragments:
    def test_preserves_input_order(self) -> None:
        # Assembly must NOT reorder: fragments are supplied in source order
        # (regression guard for the kind-bucket re-sort that scrambled output).
        frags = [
            _frag("module_header", "__header__", "import os"),
            _frag("function", "foo", "def foo(): ..."),
            _frag("class", "Bar", "class Bar: ..."),
        ]
        result = assemble_fragments(frags)
        assert result.index("import os") < result.index("def foo")
        assert result.index("def foo") < result.index("class Bar")

    def test_does_not_alphabetize(self) -> None:
        # zebra defined before apple must stay before apple after assembly.
        frags = [
            _frag("function", "zebra", "def zebra(): ..."),
            _frag("function", "apple", "def apple(): ..."),
        ]
        result = assemble_fragments(frags)
        assert result.index("def zebra") < result.index("def apple")

    def test_body_can_precede_function(self) -> None:
        frags = [
            _frag("module_header", "__header__", "import os"),
            _frag("module_body", "__body__", "print('hello')"),
            _frag("function", "foo", "def foo(): ..."),
        ]
        result = assemble_fragments(frags)
        assert result.index("import os") < result.index("print")

    def test_empty_fragments(self) -> None:
        result = assemble_fragments([])
        assert result.strip() == ""


class TestFormatWithRuff:
    def test_formats_valid_python(self) -> None:
        source = "x=1\ny =    2\n"
        formatted = format_with_ruff(source)
        assert "x = 1" in formatted

    def test_returns_original_on_bad_input(self) -> None:
        source = "not valid {{python"
        result = format_with_ruff(source)
        assert result == source or "not valid" in result


class TestReconstructFile:
    def test_round_trip_valid_python(self) -> None:
        frags = [
            _frag("module_header", "__header__", "import os"),
            _frag("function", "greet", "def greet(name):\n    return f'hello {name}'"),
        ]
        result = reconstruct_file(frags)
        assert "import os" in result
        assert "def greet" in result

    def test_writes_to_disk(self, tmp_path: Path) -> None:
        frags = [
            _frag("function", "foo", "def foo():\n    return 42"),
        ]
        out = tmp_path / "output.py"
        reconstruct_file(frags, output_path=out)
        assert out.exists()
        content = out.read_text()
        assert "def foo" in content

    def test_syntax_error_raises(self) -> None:
        frags = [
            _frag("function", "bad", "def (broken"),
        ]
        with pytest.raises(SyntaxError):
            reconstruct_file(frags)

    def test_no_ruff(self) -> None:
        frags = [
            _frag("function", "foo", "def foo():\n    return 42"),
        ]
        result = reconstruct_file(frags, use_ruff=False)
        assert "def foo" in result

    def test_round_trip_with_store(self, tmp_path: Path) -> None:
        from mak.node_store.store import NodeStore

        source = textwrap.dedent("""\
            import os

            def greet(name: str) -> str:
                return f"hello {name}"

            class Calculator:
                def add(self, a: int, b: int) -> int:
                    return a + b
        """)
        store = NodeStore(tmp_path / "ns")
        store.parse_file_into_nodes("mod.py", source)
        frags = store.get_committed_fragments("mod.py")
        result = reconstruct_file(frags, use_ruff=False)

        assert "import os" in result
        assert "def greet" in result
        assert "class Calculator" in result
