"""Tests for mak.node_store.makignore."""

from __future__ import annotations

from pathlib import Path

import pytest

from mak.node_store.ingestion import iter_source_files, walk_and_parse
from mak.node_store.makignore import (
    DEFAULT_MAKIGNORE,
    MAKIGNORE_FILENAME,
    MakIgnore,
    ensure_makignore,
    load_makignore,
)


def _ignore(*lines: str) -> MakIgnore:
    return MakIgnore(lines)


class TestMatching:
    @pytest.mark.parametrize(
        ("pattern", "path", "is_dir", "expected"),
        [
            # Unanchored names match at any depth, files and directories alike.
            ("secret.py", "secret.py", False, True),
            ("secret.py", "pkg/deep/secret.py", False, True),
            ("gen", "gen", True, True),
            ("gen", "pkg/gen", True, True),
            # A trailing slash matches directories only.
            ("gen/", "gen", True, True),
            ("gen/", "gen", False, False),
            # A slash at the start or in the middle anchors to the root.
            ("/top.py", "top.py", False, True),
            ("/top.py", "pkg/top.py", False, False),
            ("pkg/mod.py", "pkg/mod.py", False, True),
            ("pkg/mod.py", "other/pkg/mod.py", False, False),
            # Wildcards stay within one segment; ** spans several.
            ("*.pyc", "a/b.pyc", False, True),
            ("pkg/*.py", "pkg/sub/x.py", False, False),
            ("pkg/**/x.py", "pkg/x.py", False, True),
            ("pkg/**/x.py", "pkg/a/b/x.py", False, True),
            ("**/fixtures", "a/b/fixtures", True, True),
            ("pkg/**", "pkg/a/b.py", False, True),
            ("pkg/**", "pkg", True, False),
            ("test_?.py", "test_a.py", False, True),
            ("test_[ab].py", "test_c.py", False, False),
        ],
    )
    def test_pattern(
        self, pattern: str, path: str, is_dir: bool, expected: bool
    ) -> None:
        assert _ignore(pattern).matches(path, is_dir) is expected

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        ignore = _ignore("# a.py", "", "   ")
        assert not ignore
        assert not ignore.matches("a.py", False)

    def test_escaped_hash_and_bang_are_literal(self) -> None:
        assert _ignore("\\#odd.py").matches("#odd.py", False)
        assert _ignore("\\!odd.py").matches("!odd.py", False)

    def test_trailing_spaces_are_trimmed(self) -> None:
        assert _ignore("a.py   ").matches("a.py", False)

    def test_negation_reincludes_and_last_match_wins(self) -> None:
        ignore = _ignore("gen/*.py", "!gen/keep.py")
        assert ignore.matches("gen/drop.py", False)
        assert not ignore.matches("gen/keep.py", False)
        assert _ignore("!a.py", "a.py").matches("a.py", False)

    def test_file_under_an_ignored_directory_cannot_be_reincluded(self) -> None:
        ignore = _ignore("gen/", "!gen/keep.py")
        assert ignore.is_ignored("gen/keep.py")
        assert ignore.is_ignored("gen/sub/x.py")
        assert not ignore.is_ignored("src/gen.py")


class TestDefaults:
    def test_default_ignores_mak_and_git_at_any_depth(self) -> None:
        ignore = MakIgnore(DEFAULT_MAKIGNORE.splitlines())
        assert ignore.is_ignored(".mak/node_store/a.py/v1.py")
        assert ignore.is_ignored(".mak/node_store/.mak/node_store/x.py")
        assert ignore.is_ignored(".git/hooks/pre-commit.py")
        assert not ignore.is_ignored("src/mak.py")

    def test_ensure_creates_the_file_with_defaults(self, tmp_path: Path) -> None:
        path = ensure_makignore(tmp_path)
        assert path == tmp_path / MAKIGNORE_FILENAME
        assert path.read_text() == DEFAULT_MAKIGNORE

    def test_ensure_never_overwrites_a_user_file(self, tmp_path: Path) -> None:
        (tmp_path / MAKIGNORE_FILENAME).write_text("")
        ensure_makignore(tmp_path)
        assert (tmp_path / MAKIGNORE_FILENAME).read_text() == ""

    def test_load_falls_back_to_defaults_only_when_absent(
        self, tmp_path: Path
    ) -> None:
        assert load_makignore(tmp_path).is_ignored(".mak/x.py")
        (tmp_path / MAKIGNORE_FILENAME).write_text("# user emptied it\n")
        assert not load_makignore(tmp_path)


class TestWalk:
    def test_walk_prunes_ignored_directories_and_files(self, tmp_path: Path) -> None:
        (tmp_path / "keep.py").write_text("x = 1\n")
        (tmp_path / "drop.py").write_text("x = 1\n")
        nested = tmp_path / ".mak" / "node_store" / ".mak" / "node_store"
        nested.mkdir(parents=True)
        (nested / "v1.py").write_text("x = 1\n")
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "lib.py").write_text("x = 1\n")

        ignore = _ignore(".mak/", "vendor", "/drop.py")
        seen: list[str] = []

        def recording(rel: str, is_dir: bool) -> bool:
            seen.append(rel)
            return ignore.matches(rel, is_dir)

        files = iter_source_files(tmp_path, ignore=recording)
        assert [p.name for p in files] == ["keep.py"]
        # Pruned, not filtered: nothing below an ignored directory is visited.
        assert not any(rel.startswith((".mak/", "vendor/")) for rel in seen)

    def test_walk_and_parse_honors_ignore(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("x = 1\n")
        result = walk_and_parse(tmp_path, ignore=_ignore("b.py").matches)
        assert set(result) == {"a.py"}
