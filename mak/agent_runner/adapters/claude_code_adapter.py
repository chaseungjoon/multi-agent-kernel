"""Claude Code CLI adapter — a secondary/fallback agent backend.

Drives the ``claude`` CLI through the ``claude_code`` bridge wrapper. API adapters
remain primary; this exists for environments that prefer the local CLI. See
``CliSubprocessAdapter`` for the wire contract.
"""

from __future__ import annotations

from mak.agent_runner.adapters.cli_adapter import CliSubprocessAdapter


class ClaudeCodeAdapter(CliSubprocessAdapter):
    """Drives the ``claude`` CLI over the MAK protocol via its bridge wrapper."""

    agent_type = "claude_code"
    wrapper_module = "mak.agent_runner.wrappers.claude_code"
