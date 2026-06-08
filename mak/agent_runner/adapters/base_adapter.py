"""Agent adapter interfaces.

The base ``AgentAdapter`` is transport-agnostic — it only translates between
MAK's ``TaskBundle``/``TaskResult`` and a backend, which is all an API-based
adapter (Anthropic/OpenAI/Gemini SDK) needs. ``SubprocessAgentAdapter`` adds the
``spawn`` hook for CLI adapters, so API adapters are not forced to implement a
meaningless subprocess method.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod

from mak.core.types import TaskBundle, TaskResult


class AgentAdapter(ABC):
    """Transport-agnostic adapter between MAK's protocol and an agent backend."""

    agent_id: str
    agent_type: str

    @abstractmethod
    def format_task(self, task_bundle: TaskBundle) -> str:
        """Translate a TaskBundle into the request the backend expects."""
        ...

    @abstractmethod
    def parse_result(self, raw_output: str) -> TaskResult:
        """Parse the backend's response into a TaskResult."""
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Verify the backend is reachable and responsive."""
        ...


class SubprocessAgentAdapter(AgentAdapter, ABC):
    """Adapter for agents driven as CLI subprocesses over stdin/stdout."""

    @abstractmethod
    def spawn(self, working_dir: str) -> subprocess.Popen[str]:
        """Spawn the agent subprocess in ``working_dir``."""
        ...
