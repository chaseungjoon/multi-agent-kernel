"""What every optional semantic gate consumes and produces."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mak.core.types import NodeId

# ``(argv, cwd, timeout_s) -> CompletedProcess``: a seam so tests can script a
# tool's output instead of installing it.
ProcessRunner = Callable[[list[str], Path, float], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True, slots=True)
class GateFinding:
    """One problem a gate found, shaped for a fix-up task.

    ``targets`` are the nodes the fix-up should rewrite and ``context`` the
    ones it should read; ``tasks`` names the wave task(s) the problem is
    attributed to, so the fix-up can say whose work met in it.
    """

    gate: str
    file: str
    detail: str
    targets: tuple[NodeId, ...]
    context: tuple[NodeId, ...] = ()
    tasks: tuple[str, ...] = ()


@dataclass(frozen=True)
class WaveView:
    """The slice of a finished wave the gates may look at.

    ``before`` holds each touched file as it was before the wave (``None``: it
    did not exist); ``subset(tasks)`` returns file overrides that rebuild the
    pre-wave state plus only those tasks' commits; ``current(path)`` is the
    store's content of a file now.
    """

    work_dir: Path
    before: dict[str, str | None]
    current: Callable[[str], str | None]
    subset: Callable[[frozenset[str]], dict[str, str | None]]
    writers: dict[str, list[str]]
    tasks: tuple[str, ...]
    task_nodes: Callable[[str], list[NodeId]]
    file_nodes: Callable[[str], list[NodeId]]
    timeout_s: float
    max_overlays: int

    def after(self) -> dict[str, str | None]:
        """Return overrides materializing the wave's end state from the store."""
        return {path: self.current(path) for path in self.before}


def run_process(
    argv: list[str], cwd: Path, timeout_s: float
) -> subprocess.CompletedProcess[str]:
    """Run a gate subprocess: no bytecode caches, the project on ``sys.path``."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    paths = [str(cwd)]
    if (cwd / "src").is_dir():
        paths.append(str(cwd / "src"))
    env["PYTHONPATH"] = os.pathsep.join([*paths, env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    return subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True,
        timeout=timeout_s, check=False,
    )


def python() -> str:
    """Return the interpreter gates run tools under (the one running MAK)."""
    return sys.executable
