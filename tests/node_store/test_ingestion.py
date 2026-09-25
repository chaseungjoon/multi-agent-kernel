"""Tests for mak.node_store.ingestion."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mak.node_store.ingestion import (
    _is_excluded,
    iter_source_files,
    parse_file_into_fragments,
    walk_and_parse,
)

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


def _tree(root: Path) -> None:
    """Build a tree that distinguishes anchored globs from recursive ones."""
    (root / "top.py").write_text("x = 1\n")
    (root / "notes.md").write_text("# notes\n")
    (root / "src").mkdir()
    (root / "src" / "mod.py").write_text("y = 1\n")
    (root / "src" / "deep").mkdir()
    (root / "src" / "deep" / "inner.py").write_text("z = 1\n")
    (root / "src" / "test_a.py").write_text("a = 1\n")
    (root / "src" / "text_b.py").write_text("b = 1\n")
    (root / "vendor").mkdir()
    (root / "vendor" / "lib.py").write_text("c = 1\n")


def _by_glob(
    root: Path, includes: tuple[str, ...], excludes: tuple[str, ...]
) -> list[Path]:
    """Reproduce the pre-Wave-18 path: glob all, then discard the excluded."""
    out: list[Path] = []
    seen: set[Path] = set()
    for pattern in includes:
        for path in sorted(root.glob(pattern)):
            rel = str(path.relative_to(root))
            if not path.is_file() or _is_excluded(rel, excludes) or path in seen:
                continue
            seen.add(path)
            out.append(path)
    return out


class TestIterSourceFiles:
    """The pruning walk must resolve a glob exactly as ``Path.glob`` does.

    It is the only place in MAK that reimplements glob matching, and it decides
    what gets ingested — a divergence here is a file silently missing from the
    node store, which no later stage can detect.
    """

    @pytest.mark.parametrize(
        "includes",
        [
            ("**/*.py",),
            ("*.py",),
            ("src/*.py",),
            ("src/**/*.py",),
            ("**/*.py", "**/*.md"),
            ("**/te[sx]t_*.py",),
            ("**/?op.py",),
            ("nothing/**/*.py",),
        ],
    )
    @pytest.mark.parametrize(
        "excludes",
        [(), ("**/vendor/**",), ("**/deep/**", "**/*_b.py")],
    )
    def test_matches_the_glob_it_replaces(
        self, tmp_path: Path, includes: tuple[str, ...], excludes: tuple[str, ...]
    ) -> None:
        _tree(tmp_path)
        assert iter_source_files(tmp_path, includes, excludes) == _by_glob(
            tmp_path, includes, excludes
        )

    @pytest.mark.parametrize(
        ("excludes", "expected"),
        [
            (
                (),
                ["src/deep/inner.py", "src/mod.py", "src/test_a.py", "src/text_b.py"],
            ),
            (("**/deep/**", "**/*_b.py"), ["src/mod.py", "src/test_a.py"]),
        ],
    )
    def test_a_trailing_double_star_matches_every_file_below(
        self, tmp_path: Path, excludes: tuple[str, ...], expected: list[str]
    ) -> None:
        # Not compared with Path.glob: its answer for a trailing '**' changed
        # in Python 3.13 (directories only before, files too after). MAK's
        # meaning is fixed and host-independent.
        _tree(tmp_path)
        found = iter_source_files(tmp_path, ("src/**",), excludes)
        assert [p.relative_to(tmp_path).as_posix() for p in found] == expected

    def test_a_wildcard_does_not_cross_a_separator(self, tmp_path: Path) -> None:
        # The reason fnmatch.translate is unusable here: its '*' spans '/'.
        _tree(tmp_path)
        found = iter_source_files(tmp_path, ("src/*.py",), ())
        assert {p.relative_to(tmp_path).as_posix() for p in found} == {
            "src/mod.py",
            "src/test_a.py",
            "src/text_b.py",
        }

    def test_an_excluded_directory_is_not_descended_into(
        self, tmp_path: Path
    ) -> None:
        _tree(tmp_path)
        visited: list[str] = []
        original = Path.iterdir

        def spy(self: Path) -> object:
            visited.append(self.name)
            return original(self)

        Path.iterdir = spy  # type: ignore[method-assign,assignment]
        try:
            iter_source_files(tmp_path, ("**/*.py",), ("**/vendor/**",))
        finally:
            Path.iterdir = original  # type: ignore[method-assign]
        assert "vendor" not in visited
        assert "src" in visited

    def test_the_skip_predicate_prunes_a_directory(self, tmp_path: Path) -> None:
        _tree(tmp_path)
        found = iter_source_files(
            tmp_path, ("**/*.py",), (), skip=lambda p: p.name == "src"
        )
        assert {p.relative_to(tmp_path).as_posix() for p in found} == {
            "top.py",
            "vendor/lib.py",
        }

    def test_an_unreadable_directory_is_skipped_not_fatal(
        self, tmp_path: Path
    ) -> None:
        _tree(tmp_path)
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "hidden.py").write_text("q = 1\n")
        locked.chmod(0o000)
        try:
            found = iter_source_files(tmp_path, ("**/*.py",), ())
        finally:
            locked.chmod(0o755)
        assert any(p.name == "top.py" for p in found)
