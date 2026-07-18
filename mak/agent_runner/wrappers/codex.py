"""Bridge wrapper for the ``codex`` CLI.

Run as ``python -m mak.agent_runner.wrappers.codex``. Drives ``codex exec``
(non-interactive) via the shared bridge. Override the invocation with
``MAK_CODEX_CMD`` (e.g. ``"codex exec --model gpt-5.6-sol"``).
"""

from __future__ import annotations

import sys

from mak.agent_runner.wrappers.bridge import CliSpec, main

SPEC = CliSpec(
    agent_type="codex",
    cli_name="codex",
    base_argv=("codex", "exec"),
    prompt_via="arg",
)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main(SPEC))
