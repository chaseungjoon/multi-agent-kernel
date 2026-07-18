"""GitHub Copilot CLI adapter — a secondary/fallback agent backend.

Drives the ``gh copilot`` CLI through the ``copilot`` bridge wrapper. API adapters
remain primary; this exists for environments that prefer the local CLI. The
``cmd`` override selects the underlying binary the wrapper drives. See
``CliSubprocessAdapter`` for the wire contract.
"""

from __future__ import annotations

from mak.agent_runner.adapters.cli_adapter import CliSubprocessAdapter


class CopilotAdapter(CliSubprocessAdapter):
    """Drives the ``gh copilot`` CLI over the MAK protocol via its bridge wrapper."""

    agent_type = "copilot"
    wrapper_module = "mak.agent_runner.wrappers.copilot"
