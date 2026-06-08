"""Claude Code CLI adapter — a secondary/fallback agent backend.

Wraps the ``claude`` CLI. API adapters remain primary; this exists for environments
that prefer the local CLI. See ``CliSubprocessAdapter`` for the wire contract.
"""

from __future__ import annotations

from mak.agent_runner.adapters.cli_adapter import CliSubprocessAdapter


class ClaudeCodeAdapter(CliSubprocessAdapter):
    """Drives the ``claude`` CLI over the MAK newline-JSON protocol."""

    agent_type = "claude_code"
    default_command = ("claude",)
