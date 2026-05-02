"""AgentAdapter abstract base class for all agent adapters."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod

from mak.core.types import TaskBundle, TaskResult


class AgentAdapter(ABC):
    """Abstract base for agent CLI adapters."""

    agent_id: str
    agent_type: str

    @abstractmethod
    def spawn(self, working_dir: str) -> subprocess.Popen[str]:
        """Spawn a subprocess for this agent type."""
        ...

    @abstractmethod
    def format_task(self, task_bundle: TaskBundle) -> str:
        """Translate a TaskBundle into the format the CLI tool expects on stdin."""
        ...

    @abstractmethod
    def parse_result(self, raw_output: str) -> TaskResult:
        """Parse the CLI tool's stdout into a TaskResult."""
        ...

    @abstractmethod
    def health_check(self) -> bool:
        """Verify the CLI tool is installed and responsive."""
        ...
