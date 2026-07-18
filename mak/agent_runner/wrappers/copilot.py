"""Bridge wrapper for the GitHub Copilot CLI (``gh copilot``).

Run as ``python -m mak.agent_runner.wrappers.copilot``. Drives ``gh copilot``
via the shared bridge, feeding the prompt on stdin (``gh`` reads its request
there). Override the invocation with ``MAK_COPILOT_CMD``.

Note: the Copilot CLI is oriented toward shell-command suggestions rather than
free-form multi-file source rewriting, so it is the weakest of the three CLI
fallbacks for MAK's node-rewrite protocol. The bridge still emits a clean
``TaskResult`` (success or a clear error) rather than hanging.
"""

from __future__ import annotations

import sys

from mak.agent_runner.wrappers.bridge import CliSpec, main

SPEC = CliSpec(
    agent_type="copilot",
    cli_name="gh",
    base_argv=("gh", "copilot", "suggest", "-t", "shell"),
    prompt_via="stdin",
)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main(SPEC))
