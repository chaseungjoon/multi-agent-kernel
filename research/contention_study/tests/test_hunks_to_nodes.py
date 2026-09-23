"""End-to-end tests for the diff-to-node mapper, against a real git repository.

A synthetic repository is cheaper than a fixture file and exercises the same
plumbing the study uses: real ``git diff -U0 -M`` output, real blobs, real
rename detection.
"""

from __future__ import annotations

import subprocess
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from mining.diff_parse import parse_diff
from mining.git_repo import GitRepo
from mining.hunks_to_nodes import map_file_diff

BASE = textwrap.dedent(
    '''\
    """Doc."""

    import os


    def alpha(a):
        return a


    def beta(b):
        return b


    class Widget:
        """Shell."""

        def method_one(self):
            return 1

        def method_two(self):
            return 2
    '''
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Iterator[tuple[GitRepo, Path]]:
    """Build a one-commit repository containing ``mod.py``."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", ".")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "mod.py").write_text(BASE)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    yield GitRepo(work / ".git"), work


def _touches(repo: GitRepo, work: Path, message: str) -> dict[str, tuple[str, bool]]:
    """Commit the working tree and map the resulting diff into node touches."""
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", message)
    head = repo.text("rev-parse", "HEAD").strip()
    base = repo.text("rev-parse", "HEAD~1").strip()
    mapped: dict[str, tuple[str, bool]] = {}
    for diff in parse_diff(repo.diff_u0(base, head)):
        touches, _failed = map_file_diff(repo, base, head, diff)
        for touch in touches:
            mapped[touch.node_id] = (touch.change_type, touch.append_only)
    return mapped


def test_edit_inside_one_function_touches_only_that_function(
    repo: tuple[GitRepo, Path]
) -> None:
    handle, work = repo
    (work / "mod.py").write_text(BASE.replace("    return a\n", "    return a + 1\n"))
    mapped = _touches(handle, work, "edit alpha")
    assert set(mapped) == {"mod.py::function::alpha"}
    assert mapped["mod.py::function::alpha"] == ("modify", False)


def test_new_import_touches_only_the_header_and_is_append_only(
    repo: tuple[GitRepo, Path]
) -> None:
    handle, work = repo
    (work / "mod.py").write_text(BASE.replace("import os\n", "import os\nimport sys\n"))
    mapped = _touches(handle, work, "add import")
    assert set(mapped) == {"mod.py::module_header::__header__"}
    assert mapped["mod.py::module_header::__header__"] == ("modify", True)


def test_new_function_is_a_new_node_not_an_edit_of_its_neighbour(
    repo: tuple[GitRepo, Path]
) -> None:
    handle, work = repo
    (work / "mod.py").write_text(BASE + "\n\ndef gamma(c):\n    return c\n")
    mapped = _touches(handle, work, "add gamma")
    assert "mod.py::function::gamma" in mapped
    assert mapped["mod.py::function::gamma"][0] == "add"
    assert "mod.py::function::beta" not in mapped


def test_deleting_a_function_records_a_deletion(repo: tuple[GitRepo, Path]) -> None:
    handle, work = repo
    (work / "mod.py").write_text(BASE.replace("def beta(b):\n    return b\n\n\n", ""))
    mapped = _touches(handle, work, "drop beta")
    assert mapped.get("mod.py::function::beta", ("", False))[0] == "delete"


def test_editing_one_method_does_not_touch_its_sibling(
    repo: tuple[GitRepo, Path]
) -> None:
    handle, work = repo
    edited = BASE.replace("        return 1\n", "        return 11\n")
    (work / "mod.py").write_text(edited)
    mapped = _touches(handle, work, "edit method_one")
    assert set(mapped) == {"mod.py::method::Widget.method_one"}
    assert "mod.py::method::Widget.method_two" not in mapped


def test_two_edits_in_different_functions_are_two_nodes(
    repo: tuple[GitRepo, Path]
) -> None:
    handle, work = repo
    updated = BASE.replace("    return a\n", "    return a + 1\n").replace(
        "    return b\n", "    return b + 2\n"
    )
    (work / "mod.py").write_text(updated)
    mapped = _touches(handle, work, "edit both")
    assert set(mapped) == {"mod.py::function::alpha", "mod.py::function::beta"}


def test_non_python_file_is_kept_as_one_whole_file_node(
    repo: tuple[GitRepo, Path]
) -> None:
    handle, work = repo
    (work / "README.md").write_text("hello\n")
    mapped = _touches(handle, work, "add readme")
    assert "README.md::__file__::__whole__" in mapped


def test_rename_is_keyed_by_the_new_path(repo: tuple[GitRepo, Path]) -> None:
    handle, work = repo
    edited = BASE.replace("    return a\n", "    return a + 1\n")
    (work / "renamed.py").write_text(edited)
    (work / "mod.py").unlink()
    mapped = _touches(handle, work, "rename and edit")
    assert any(key.startswith("renamed.py::") for key in mapped)
    assert not any(key.startswith("mod.py::") for key in mapped)
