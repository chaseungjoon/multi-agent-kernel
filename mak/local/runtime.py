"""What a local model server *is*, and how MAK asks one whether it is there.

A ``LocalRuntime`` is the answer to one question — "something is listening at
this URL; what is it and what does it have?" — captured as a value object rather
than a live handle, so the TUI can hold, list, and compare probe results without
holding connections open.

Two kinds, and the distinction is load-bearing rather than cosmetic:

``ollama``
    Ollama's native API. MAK supports it natively (see the ``ollama_api``
    adapter) because it is the only local runtime that lets a request size its
    own context window — and an undersized context is the one local failure mode
    that produces confident garbage with nothing in any log to explain it.

``openai_compatible``
    Everything else — LM Studio, vLLM, llama.cpp's server, LocalAI. All of them
    speak Chat Completions, all of them are reached through ``local_api``, and
    none of them needs its own adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mak.local.ollama_client import OllamaClient, OllamaError

KIND_OLLAMA = "ollama"
KIND_OPENAI_COMPATIBLE = "openai_compatible"

# Ollama identifies itself; every other runtime is asked the one question they
# all answer, so a probe that finds a model list but no ``/api/version`` reports
# the generic kind rather than guessing at a product name.
_GENERIC_NAME = "OpenAI-compatible server"


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    """A local model server that answered a probe."""

    kind: str
    name: str
    base_url: str
    version: str | None = None
    models: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        """Return a one-line summary for the TUI: name, endpoint, model count."""
        version = f" {self.version}" if self.version else ""
        count = len(self.models)
        plural = "" if count == 1 else "s"
        return f"{self.name}{version} at {self.base_url} — {count} model{plural}"


def probe_ollama(
    base_url: str, *, timeout: float, client: OllamaClient | None = None
) -> LocalRuntime | None:
    """Return the Ollama runtime at ``base_url``, or None if none answered.

    Never raises. Probing runs on a UI path and during startup discovery, where
    an unreachable endpoint is an ordinary, expected answer — not a failure that
    may take a session down with it.
    """
    api = client or OllamaClient(base_url, timeout=timeout)
    try:
        version = api.version()
    except OllamaError:
        return None
    try:
        models = tuple(model.name for model in api.list_models())
    except OllamaError:
        # Reachable but not listing: still a runtime, just an empty one. Saying
        # "nothing is running" here would be a lie the user cannot debug.
        models = ()
    return LocalRuntime(
        kind=KIND_OLLAMA,
        name="Ollama",
        base_url=base_url,
        version=version or None,
        models=models,
    )


def probe_openai_compatible(
    base_url: str, *, timeout: float, name: str = _GENERIC_NAME
) -> LocalRuntime | None:
    """Return the OpenAI-compatible runtime at ``base_url``, or None.

    ``base_url`` is the API root (``…/v1``); ``/models`` under it is the one
    endpoint every server in this family implements. Never raises, for the same
    reason :func:`probe_ollama` does not.
    """
    # Imported here rather than at module scope: this is the only place in
    # ``mak.local`` that touches HTTP outside the Ollama client, and keeping the
    # import local keeps the module's dependency story ("stdlib only") obvious.
    import json
    import urllib.error
    import urllib.request

    url = f"{base_url.rstrip('/')}/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    models: tuple[str, ...] = ()
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        found = [
            entry.get("id")
            for entry in payload["data"]
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]
        models = tuple(str(model) for model in found)
    return LocalRuntime(
        kind=KIND_OPENAI_COMPATIBLE,
        name=name,
        base_url=base_url,
        version=None,
        models=models,
    )
