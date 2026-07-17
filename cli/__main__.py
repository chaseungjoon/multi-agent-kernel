"""Entry point: ``python -m cli`` or the ``mak`` console script.

``mak`` with no arguments opens the interactive TUI. ``mak run --task "..."``
forwards to the one-shot kernel CLI (``python -m mak``), so installed users get
both surfaces from a single command. ``mak --version`` prints the version.
"""
from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]

    if argv and argv[0] == "run":
        from mak.__main__ import main as run_main
        return run_main(argv[1:])

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

    from cli.app import MakCli
    MakCli().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
