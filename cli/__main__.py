"""Entry point: ``python -m cli`` or the ``mak`` console script."""
from __future__ import annotations

import sys


def main() -> None:
    from cli.app import MakCli
    MakCli().run()


if __name__ == "__main__":
    sys.exit(main() or 0)
