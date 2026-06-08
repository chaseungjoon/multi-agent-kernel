"""Shared base for CLI subprocess adapters (secondary/fallback agents).

The primary agent path is the direct-API adapters. CLI adapters are a fallback for
backends with no stable API; they are driven by the agent runner's idle-process
pool over the MAK wire protocol: the runner writes one ``TaskBundle`` JSON line to
the process's stdin and reads one ``TaskResult`` JSON line back from stdout.

``CliSubprocessAdapter`` implements that contract once. A concrete adapter only
declares its ``agent_type`` and ``default_command`` (the argv to launch). Because
real CLIs do not natively speak MAK's line protocol, a deployment typically points
``cmd`` at a thin wrapper that adapts the CLI's I/O — the adapter does not assume a
particular CLI's flags.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from mak.agent_runner.adapters.base_adapter import SubprocessAgentAdapter
from mak.agent_runner.protocol import decode_task_result, encode_task_bundle
from mak.agent_runner.sandbox import SandboxConfig
from mak.core.types import TaskBundle, TaskResult

_HEALTH_TIMEOUT_S = 10.0


class CliSubprocessAdapter(SubprocessAgentAdapter):
    """Drives a CLI agent over the MAK newline-JSON protocol via stdin/stdout."""

    agent_type = "cli"
    default_command: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        cmd: str | None = None,
        agent_id: str | None = None,
        sandbox: SandboxConfig | None = None,
    ) -> None:
        base = list(command) if command is not None else list(self.default_command)
        if cmd is not None:
            base = [cmd, *base[1:]] if base else [cmd]
        self._command = base
        self._sandbox = sandbox
        self.agent_id = agent_id or f"{self.agent_type}-0"

    @property
    def command(self) -> list[str]:
        """The argv used to launch the agent (before any sandbox wrapping)."""
        return list(self._command)

    def spawn(self, working_dir: str) -> subprocess.Popen[str]:
        """Launch the CLI agent with stdin/stdout pipes, optionally sandboxed."""
        argv = self._command
        if self._sandbox is not None:
            argv = self._sandbox.wrap(argv, working_dir)
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            cwd=working_dir,
        )

    def format_task(self, task_bundle: TaskBundle) -> str:
        """Serialize the task bundle to the JSON line written to stdin."""
        return encode_task_bundle(task_bundle)

    def parse_result(self, raw_output: str) -> TaskResult:
        """Decode the JSON line read from stdout into a ``TaskResult``."""
        return decode_task_result(raw_output)

    def health_check(self) -> bool:
        """Return whether the CLI binary is installed and responsive."""
        if not self._command:
            return False
        try:
            result = subprocess.run(
                [self._command[0], *self.version_args],
                capture_output=True,
                timeout=_HEALTH_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0
