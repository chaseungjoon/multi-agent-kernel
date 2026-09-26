"""Size budgets for the session package: growth is a reviewed decision.

``Session`` grew to thousands of lines because every feature that touched a
run added a method to it. These budgets make that visible: the state machine
stays small, no collaborator becomes the next catch-all, and no function hides a
second responsibility inside itself. The allowlist is the only exception
mechanism, and every entry must say why.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "mak" / "session"
CORE_MAX_LINES = 600
MODULE_MAX_LINES = 800
FUNCTION_MAX_LINES = 80

# "<path relative to mak/session>" or "<path>::<qualified function name>"
# mapped to the reason it may exceed its budget. Keep it empty if you can.
ALLOWLIST: dict[str, str] = {}


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(PACKAGE).as_posix()


def _functions(path: Path) -> list[tuple[str, int]]:
    """Every function in ``path`` (at any depth) with its length in lines."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                start = min(
                    [d.lineno for d in child.decorator_list], default=child.lineno
                )
                name = f"{prefix}{child.name}"
                found.append((name, (child.end_lineno or start) - start + 1))
                visit(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")

    visit(tree, "")
    return found


def test_the_package_exists() -> None:
    assert (PACKAGE / "core.py").is_file()
    assert len(_modules()) > 10


def test_session_core_stays_a_thin_state_machine() -> None:
    lines = len((PACKAGE / "core.py").read_text(encoding="utf-8").splitlines())
    if "core.py" in ALLOWLIST:
        pytest.skip(ALLOWLIST["core.py"])
    assert lines <= CORE_MAX_LINES, (
        f"mak/session/core.py has {lines} lines (budget {CORE_MAX_LINES}): move the "
        "new responsibility into a collaborator"
    )


@pytest.mark.parametrize("path", _modules(), ids=_rel)
def test_no_module_is_the_next_catch_all(path: Path) -> None:
    lines = len(path.read_text(encoding="utf-8").splitlines())
    if _rel(path) in ALLOWLIST:
        return
    assert lines <= MODULE_MAX_LINES, (
        f"mak/session/{_rel(path)} has {lines} lines (budget {MODULE_MAX_LINES})"
    )


@pytest.mark.parametrize("path", _modules(), ids=_rel)
def test_no_function_hides_a_second_responsibility(path: Path) -> None:
    too_long = [
        f"{name} ({length} lines)"
        for name, length in _functions(path)
        if length > FUNCTION_MAX_LINES and f"{_rel(path)}::{name}" not in ALLOWLIST
    ]
    assert not too_long, (
        f"mak/session/{_rel(path)}: over {FUNCTION_MAX_LINES} lines: "
        + ", ".join(too_long)
    )


def test_every_allowlist_entry_names_something_real_and_says_why() -> None:
    names = {_rel(p) for p in _modules()} | {
        f"{_rel(p)}::{name}" for p in _modules() for name, _ in _functions(p)
    }
    for entry, reason in ALLOWLIST.items():
        assert entry in names, f"allowlist entry {entry!r} matches nothing"
        assert reason.strip(), f"allowlist entry {entry!r} gives no reason"
