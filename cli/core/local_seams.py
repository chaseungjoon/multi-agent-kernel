"""The three ways ``/local`` reaches a runtime, as one injectable value.

Discovery, the Ollama client, and the saved-host probe used to be module
globals reassigned through ``global`` by ``set_seams``/``reset_seams``. A test
that forgot to reset them leaked a fake runtime into every later test. They are
now a :class:`LocalSeams` the app holds on its ``CliState``; a test builds a
state with its own seams and nothing module-level changes.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mak.local import LocalRuntime, OllamaClient, discover
from mak.local.runtime import KIND_OLLAMA, probe_ollama, probe_openai_compatible

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cli.core.state import LocalHost

DiscoverFn = Callable[[], list[LocalRuntime]]
ClientFactory = Callable[[str], OllamaClient]
HostProbe = Callable[["LocalHost"], "LocalRuntime | None"]

# A saved remote host is asked whether it is up on a UI path, so the answer
# must come back in seconds rather than after the client's generation timeout.
HOST_PROBE_TIMEOUT_S = 2.0


def default_discover() -> list[LocalRuntime]:
    """Scan the well-known local endpoints. Never raises."""
    return discover()


def default_client(base_url: str) -> OllamaClient:
    """Build a client for ``base_url``."""
    return OllamaClient(base_url)


def default_probe_host(host: LocalHost) -> LocalRuntime | None:
    """Ask a saved host whether it is up and what it has. Never raises."""
    if host.kind == KIND_OLLAMA:
        return probe_ollama(host.url, timeout=HOST_PROBE_TIMEOUT_S)
    return probe_openai_compatible(
        host.url, timeout=HOST_PROBE_TIMEOUT_S, name="OpenAI-compatible server"
    )


@dataclass(frozen=True)
class LocalSeams:
    """How ``/local`` discovers runtimes, talks to one, and probes a saved host."""

    discover: DiscoverFn = default_discover
    client_factory: ClientFactory = default_client
    probe_host: HostProbe = default_probe_host

    @classmethod
    def default(cls) -> LocalSeams:
        """Return the real network-backed seams."""
        return cls()
