"""Tests for mak.node_store.ingestion."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mak.node_store.ingestion import parse_file_into_fragments, walk_and_parse

SAMPLE_SOURCE = textwrap.dedent("""\
    import os
    from pathlib import Path

    MAX_SIZE = 100

    def greet(name: str) -> str:
        return f"hello {name}"

    class Calculator:
        def add(self, a: int, b: int) -> int:
            return a + b

        def sub(self, a: int, b: int) -> int:
            return a - b

    def farewell() -> str:
        return "goodbye"
""")


class TestParseFileIntoFragments:
    def test_extracts_header(self) -> None:
        frags = parse_file_into_fragments("sample.py", SAMPLE_SOURCE)
        headers = [f for f in frags if f.kind == "module_header"]
        assert len(headers) == 1
        assert "import os" in headers[0].source
        assert "MAX_SIZE = 100" in headers[0].source

    def test_extracts_functions(self) -> None:
        frags = parse_file_into_fragments("sample.py", SAMPLE_SOURCE)
        funcs = [f for f in frags if f.kind == "function"]
        names = {f.node_id for f in funcs}
        assert "sample.py::function::greet" in names
        assert "sample.py::function::farewell" in names

    def test_extracts_class(self) -> None:
        frags = parse_file_into_fragments("sample.py", SAMPLE_SOURCE)
        classes = [f for f in frags if f.kind == "class"]
        assert len(classes) == 1
        assert "Calculator" in classes[0].source

    def test_all_fragments_have_version_1(self) -> None:
        frags = parse_file_into_fragments("sample.py", SAMPLE_SOURCE)
        assert all(f.version == 1 for f in frags)

    def test_node_ids_are_qualified(self) -> None:
        frags = parse_file_into_fragments("sample.py", SAMPLE_SOURCE)
        for frag in frags:
            assert str(frag.node_id).startswith("sample.py::")

    def test_empty_file(self) -> None:
        frags = parse_file_into_fragments("empty.py", "")
        assert frags == []

    def test_imports_only(self) -> None:
        source = "import os\nimport sys\n"
        frags = parse_file_into_fragments("imports.py", source)
        assert len(frags) == 1
        assert frags[0].kind == "module_header"

    def test_syntax_error_raises(self) -> None:
        with pytest.raises(SyntaxError):
            parse_file_into_fragments("bad.py", "def (broken")

    def test_reads_from_file(self, tmp_path: Path) -> None:
        src = "def foo() -> int:\n    return 42\n"
        p = tmp_path / "mod.py"
        p.write_text(src)
        frags = parse_file_into_fragments(str(p))
        assert len(frags) == 1
        assert frags[0].kind == "function"


class TestWalkAndParse:
    def test_walks_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("def hello(): ...\n")
        (tmp_path / "b.py").write_text("class Foo: ...\n")
        (tmp_path / "c.txt").write_text("not python")
        result = walk_and_parse(tmp_path)
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    def test_excludes_patterns(self, tmp_path: Path) -> None:
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "x.py").write_text("def hidden(): ...\n")
        (tmp_path / "ok.py").write_text("def visible(): ...\n")
        result = walk_and_parse(
            tmp_path, exclude_patterns=("**/.venv/**",)
        )
        assert "ok.py" in result
        assert ".venv/x.py" not in result

    def test_skips_syntax_errors(self, tmp_path: Path) -> None:
        (tmp_path / "bad.py").write_text("def (broken")
        (tmp_path / "good.py").write_text("x = 1\n")
        result = walk_and_parse(tmp_path)
        assert "bad.py" not in result
        assert "good.py" in result
