"""GitHub Copilot CLI adapter — a secondary/fallback agent backend.

Wraps the ``gh copilot`` CLI. API adapters remain primary; this exists for
environments that prefer the local CLI. The ``cmd`` override replaces the ``gh``
binary. See ``CliSubprocessAdapter`` for the wire contract.
"""

from __future__ import annotations

from mak.agent_runner.adapters.cli_adapter import CliSubprocessAdapter


class CopilotAdapter(CliSubprocessAdapter):
    """Drives the ``gh copilot`` CLI over the MAK newline-JSON protocol."""

    agent_type = "copilot"
    default_command = ("gh", "copilot")
