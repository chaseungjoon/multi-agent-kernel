"""Entry point: ``python -m cli`` or the ``mak`` console script.

``mak`` with no arguments opens the interactive TUI. ``mak run --task "..."``
forwards to the one-shot kernel CLI (``python -m mak``). ``mak update``
re-runs the uv install command so the tool moves to the latest revision —
updating is always explicit; launching mak never touches the network.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_URL = "https://github.com/chaseungjoon/multi-agent-kernel"
_REPO_SPEC = f"git+{_REPO_URL}"


def _is_uv_tool_install() -> bool:
    """Whether this process runs from a ``uv tool`` environment.

    uv tool venvs live under ``.../uv/tools/<name>/``; a source checkout or a
    plain pip/venv install does not, and must never be "updated" by installing
    a second copy from GitHub over it.
    """
    return "/uv/tools/" in sys.executable.replace("\\", "/")


def _installed_commit() -> str | None:
    """Return the git commit this install was built from, or None if unknown.

    uv records the resolved revision in the package's PEP 610
    ``direct_url.json`` (``vcs_info.commit_id``) inside the tool environment's
    site-packages; the tool root is two levels above the interpreter.
    """
    # No resolve(): the venv's bin/python is a symlink to the base interpreter,
    # and resolving it would escape the tool directory entirely.
    root = Path(sys.executable).parent.parent
    try:
        path = next(root.rglob("multi_agent_kernel-*.dist-info/direct_url.json"))
        commit = json.loads(path.read_text(encoding="utf-8"))["vcs_info"]["commit_id"]
    except (StopIteration, OSError, ValueError, KeyError, TypeError):
        return None
    return commit if isinstance(commit, str) and commit else None


def _remote_commit() -> str | None:
    """Return the repo's current HEAD commit, or None if it can't be fetched."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "ls-remote", _REPO_URL, "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.split()
    return head[0] if head and re.fullmatch(r"[0-9a-f]{40}", head[0]) else None


def _update() -> int:
    """Update mak to the newest revision (``mak update``).

    ``uv tool install`` with a git spec reinstalls even when the resolved
    commit is unchanged, so uv's own "Installed …" output does not mean an
    update happened. The real signal is the installed commit (PEP 610
    ``direct_url.json``): a fast ``git ls-remote`` pre-check skips the
    reinstall entirely when already current, and otherwise the installed
    commit is compared before/after to report honestly.
    """
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

    installed = _installed_commit()
    if installed is not None and installed == _remote_commit():
        print("mak: already up to date.")
        return 0

    print("mak: updating to the newest version…")
    try:
        result = subprocess.run(
            [uv, "tool", "install", _REPO_SPEC], capture_output=True, text=True
        )
    except OSError as exc:
        print(f"mak: update failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        print("mak: update failed.", file=sys.stderr)
        return result.returncode

    after = _installed_commit()
    if installed is not None and after == installed:
        print("mak: already up to date.")
    elif after is not None:
        print(
            f"mak: updated to {after[:8]} — restart mak to use the newest version."
        )
    else:
        print("mak: update complete — restart mak to use the newest version.")
    return 0


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
