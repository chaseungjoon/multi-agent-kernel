"""Bridge wrapper for the ``claude`` CLI (Claude Code).

Run as ``python -m mak.agent_runner.wrappers.claude_code``. Drives ``claude -p``
(non-interactive print mode) via the shared bridge. Override the invocation with
``MAK_CLAUDE_CODE_CMD`` (e.g. ``"claude -p --model claude-opus-4-8"``).
"""

from __future__ import annotations

import sys

from mak.agent_runner.wrappers.bridge import CliSpec, main

SPEC = CliSpec(
    agent_type="claude_code",
    cli_name="claude",
    base_argv=("claude", "-p"),
    prompt_via="arg",
)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main(SPEC))
