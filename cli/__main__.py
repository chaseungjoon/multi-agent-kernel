"""Entry point: ``python -m cli`` or the ``mak`` console script.

``mak`` with no arguments opens the interactive TUI. ``mak run --task "..."``
forwards to the one-shot kernel CLI (``python -m mak``), so installed users get
both surfaces from a single command. ``mak --version`` prints the version.

On startup (except for ``--version``/``--help``), a uv-tool install re-runs its
own install command so users stay on the latest revision. The check is
best-effort and silent: it only fires when this process actually runs out of a
``uv tool`` environment (never a source checkout), swallows every failure
(no uv, offline, timeout), and can be disabled with ``MAK_NO_SELF_UPDATE=1``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

_REPO_SPEC = "git+https://github.com/chaseungjoon/multi-agent-kernel"
_UPDATE_TIMEOUT_S = 15.0


def _is_uv_tool_install() -> bool:
    """Whether this process runs from a ``uv tool`` environment.

    uv tool venvs live under ``.../uv/tools/<name>/``; a source checkout or a
    plain pip/venv install does not, and must never be "updated" by installing
    a second copy from GitHub over it.
    """
    return "/uv/tools/" in sys.executable.replace("\\", "/")


def _self_update() -> None:
    """Re-run the uv install command so the next launch is the latest revision."""
    if os.environ.get("MAK_NO_SELF_UPDATE"):
        return
    if not _is_uv_tool_install():
        return
    uv = shutil.which("uv")
    if uv is None:
        return
    try:
        result = subprocess.run(
            [uv, "tool", "install", _REPO_SPEC],
            capture_output=True,
            text=True,
            timeout=_UPDATE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return  # offline, hung, or uv broke — never block startup on the check
    if result.returncode == 0 and "Installed" in (result.stderr + result.stdout):
        # uv prints "Installed N executables: ..." only when something changed;
        # an already-current install says "... is already installed" instead.
        print(
            "mak: updated to the latest version — the update applies on the "
            "next launch.",
            file=sys.stderr,
        )


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
            "       mak --version       print the version"
        )
        return 0

    _self_update()

    if argv and argv[0] == "run":
        from mak.__main__ import main as run_main
        return run_main(argv[1:])

    from cli.app import MakCli
    MakCli().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
