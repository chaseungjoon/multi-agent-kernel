"""The interactive app runs the same cascade guard the CLI does (Wave 16, 16.3).

A guard that fires on one of two front ends is not a guard: the cross-module check
existed for a whole wave while every TUI run skipped it.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

from cli.app import MakCli
from cli.core.state import CliState
from rich.console import Console

from mak.core.types import NodeId, SubTask

_APP = Path(__file__).resolve().parents[1] / "cli" / "app.py"


def _task(task_id: str) -> SubTask:
    return SubTask(
        task_id=task_id,
        description=task_id,
        target_nodes=[NodeId(f"{task_id}.py")],
    )


def _bare_cli(*, no_review: bool, confirm: bool) -> MakCli:
    """Build a MakCli with just the attributes the cascade hooks touch."""
    cli = MakCli.__new__(MakCli)
    cli.console = Console(file=io.StringIO(), highlight=False)
    cli.state = CliState(no_review=no_review)
    cli._confirm_plan = lambda: confirm  # type: ignore[method-assign]
    return cli


def test_execute_task_drives_the_shared_cascade_loop() -> None:
    # Structural, because _execute_task is a UI method with threads and spinners:
    # what matters is that it calls the shared driver rather than skipping it.
    tree = ast.parse(_APP.read_text())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "run_cascade_waves" in called


def test_approval_accepts_a_confirmed_plan() -> None:
    cli = _bare_cli(no_review=False, confirm=True)
    tasks = [_task("fix_a")]
    assert cli._cascade_approval()(tasks) == tasks


def test_approval_declines_when_the_user_says_no() -> None:
    cli = _bare_cli(no_review=False, confirm=False)
    assert cli._cascade_approval()([_task("fix_a")]) is None


def test_no_review_skips_the_cascade_wave() -> None:
    # Matches the CLI: --no-review means "don't ask me", not "run it unreviewed".
    cli = _bare_cli(no_review=True, confirm=True)
    assert cli._cascade_approval()([_task("fix_a")]) is None


def test_announce_renders_without_raising() -> None:
    cli = _bare_cli(no_review=False, confirm=True)
    cli._announce_cascade([_task("fix_a"), _task("fix_b")])
