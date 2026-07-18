"""Codex CLI adapter — a secondary/fallback agent backend.

Drives the ``codex`` CLI through the ``codex`` bridge wrapper. API adapters remain
primary; this exists for environments that prefer the local CLI. See
``CliSubprocessAdapter`` for the wire contract.
"""

from __future__ import annotations

from mak.agent_runner.adapters.cli_adapter import CliSubprocessAdapter


class CodexAdapter(CliSubprocessAdapter):
    """Drives the ``codex`` CLI over the MAK protocol via its bridge wrapper."""

    agent_type = "codex"
    wrapper_module = "mak.agent_runner.wrappers.codex"
