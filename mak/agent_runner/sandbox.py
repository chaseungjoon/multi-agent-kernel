"""Docker-based process isolation for CLI-type agent subprocesses.

CLI agents are arbitrary external processes — an attack surface. ``SandboxConfig``
wraps a subprocess command so it runs inside a Docker container with its filesystem
scoped to the working directory and its network restricted by default. API adapters
(the primary path) make no subprocess and are never sandboxed.

This module only *builds* the ``docker run`` argv and checks whether Docker is
available; it does not require Docker to import or to construct a config, so it is
unit-testable without a Docker daemon. A CLI adapter that is given a ``SandboxConfig``
calls ``wrap`` in its ``spawn`` to launch the agent inside the container.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_VERSION_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """How to run a CLI agent inside a Docker container.

    ``network`` is passed straight to ``docker run --network``; the default
    ``"none"`` denies all network. To permit approved API endpoints, run the
    container on a network you control (e.g. one fronted by an egress proxy) and
    pass its name here — host-level allowlisting is out of scope for this flag.
    """

    image: str = "python:3.11-slim"
    network: str = "none"
    container_workdir: str = "/work"
    docker_bin: str = "docker"
    extra_args: tuple[str, ...] = ()

    def wrap(self, argv: Sequence[str], working_dir: str) -> list[str]:
        """Return the ``docker run`` argv that runs ``argv`` in the sandbox.

        The host ``working_dir`` is bind-mounted to ``container_workdir`` and made
        the container's working directory, so the agent sees exactly the project
        tree and nothing else. ``-i`` keeps stdin open for the MAK wire protocol.
        """
        host_dir = str(Path(working_dir).resolve())
        return [
            self.docker_bin,
            "run",
            "--rm",
            "-i",
            "--network",
            self.network,
            "--volume",
            f"{host_dir}:{self.container_workdir}",
            "--workdir",
            self.container_workdir,
            *self.extra_args,
            self.image,
            *argv,
        ]


def docker_available(docker_bin: str = "docker") -> bool:
    """Return whether the Docker CLI is installed and responsive."""
    try:
        result = subprocess.run(
            [docker_bin, "--version"],
            capture_output=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
