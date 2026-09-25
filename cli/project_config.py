"""Offer to give a project its own ``.mak/config.yaml``.

MAK discovers a project's config at ``<work dir>/.mak/config.yaml`` first (see
``mak.config.discover_config_path``). When the app starts in, or ``/work-dir``
moves to, a directory with no ``.mak/`` yet, it asks once whether to create one,
seeded from the user's own config (``~/.config/mak/config.yaml``) or, failing
that, the packaged default.

Declining writes nothing. The run creates ``.mak/`` for its state on demand,
exactly as before, and the config keeps coming from the user-level file.
Nothing here overwrites an existing file: MAK's standing rule is that it never
writes a config without an explicit yes (CONTRIBUTING §11).
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from cli.core.state import CliState
from cli.ui import print_error, print_ok
from mak.config import (
    MAK_DIR_NAME,
    packaged_config_path,
    project_config_path,
    seed_config_path,
)

# Asks a yes/no question; True only on an explicit yes.
ConfirmFn = Callable[[str], bool]


def needs_project_config(work_dir: Path) -> bool:
    """Return whether ``work_dir`` exists but has no ``.mak/`` directory yet."""
    return work_dir.is_dir() and not (work_dir / MAK_DIR_NAME).exists()


def create_project_config(work_dir: Path, seed: Path) -> Path:
    """Create ``<work_dir>/.mak/config.yaml`` as a copy of ``seed``; return its path.

    An existing config is left untouched and returned as is.
    """
    target = project_config_path(work_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copyfile(seed, target)
    return target


def _seed_label(seed: Path) -> str:
    """Name the seed file the way the user knows it."""
    if seed == packaged_config_path():
        return "MAK's default config"
    try:
        return "~/" + str(seed.relative_to(Path.home()))
    except ValueError:
        return str(seed)


def confirm_in_terminal(question: str) -> bool:
    """Ask ``question`` on the terminal; Enter or anything but y/yes is no."""
    from prompt_toolkit import prompt as pt_prompt

    try:
        answer = pt_prompt(f"  {question} [y/N] ❯ ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in ("y", "yes")


def offer_project_config(
    state: CliState, console: Console, confirm: ConfirmFn = confirm_in_terminal
) -> Path | None:
    """Ask to create the work dir's ``.mak/config.yaml``; return it if created.

    Asks nothing when the work dir already has a ``.mak/``, or when the user
    pinned an explicit config with ``/config``.
    """
    work_dir = Path(state.work_dir or ".").resolve()
    if state.config_path or not needs_project_config(work_dir):
        return None
    seed = seed_config_path()
    console.print(
        f"\n  [dim]{state.work_dir_display()} has no {MAK_DIR_NAME}/ directory "
        "yet.[/dim]"
    )
    question = f"Create {MAK_DIR_NAME}/config.yaml here from {_seed_label(seed)}?"
    if not confirm(question):
        console.print(
            f"  [dim]Skipped — {MAK_DIR_NAME}/ is created on the first run, and "
            "the config comes from your user-level file.[/dim]\n"
        )
        return None
    try:
        path = create_project_config(work_dir, seed)
    except OSError as exc:
        print_error(console, f"could not create {project_config_path(work_dir)}: {exc}")
        return None
    print_ok(console, f"Created {path}")
    console.print()
    return path
