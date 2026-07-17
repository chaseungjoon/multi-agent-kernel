"""Entry point: ``python -m cli`` or the ``mak`` console script.

``mak`` with no arguments opens the interactive TUI. ``mak run --task "..."``
forwards to the one-shot kernel CLI (``python -m mak``). ``mak update``
re-runs the uv install command so the tool moves to the latest revision —
updating is always explicit; launching mak never touches the network.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

_REPO_SPEC = "git+https://github.com/chaseungjoon/multi-agent-kernel"


def _is_uv_tool_install() -> bool:
    """Whether this process runs from a ``uv tool`` environment.

    uv tool venvs live under ``.../uv/tools/<name>/``; a source checkout or a
    plain pip/venv install does not, and must never be "updated" by installing
    a second copy from GitHub over it.
    """
    return "/uv/tools/" in sys.executable.replace("\\", "/")


def _update() -> int:
    """Update mak by re-running its uv install command (``mak update``)."""
    if not _is_uv_tool_install():
        print(
            "mak: this copy is not a `uv tool` install — update your source "
            "checkout with `git pull` (then `pip install -e .`) instead.",
            file=sys.stderr,
        )
        return 1
    uv = shutil.which("uv")
    if uv is None:
        print("mak: `uv` was not found on PATH; cannot update.", file=sys.stderr)
        return 1
    # Stream uv's own output so the user sees what happened; uv reports both
    # "already installed" and a fresh install clearly.
    result = subprocess.run([uv, "tool", "install", _REPO_SPEC])
    if result.returncode == 0:
        print("mak: update complete — changes apply on the next launch.")
    return result.returncode


def main() -> int:
    argv = sys.argv[1:]

    if argv and argv[0] in ("--version", "-V"):
        from mak._version import __version__
        print(f"mak {__version__}")
        return 0

    if argv and argv[0] in ("--help", "-h"):
        print(
            "usage: mak                 launch the interactive TUI\n"
            "       mak run --task ...  run one task non-interactively "
            "(see: mak run --help)\n"
            "       mak update          update mak to the newest version\n"
            "       mak --version       print the version"
        )
        return 0

    if argv and argv[0] == "update":
        return _update()

    if argv and argv[0] == "run":
        from mak.__main__ import main as run_main
        return run_main(argv[1:])

    from cli.app import MakCli
    MakCli().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
