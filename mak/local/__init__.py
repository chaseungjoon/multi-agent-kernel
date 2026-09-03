"""Local model runtimes: detection, native transport, and suggestions.

The counterpart to ``mak/models/`` for models that are not hosted. They are kept
apart deliberately. Every provider in ``mak/models/`` is keyed by an API-key
environment variable and refreshed from that provider's hosted list-models
endpoint; a keyless, per-user-URL "provider" would have to touch
``PROVIDER_ORDER``, ``PROVIDER_KEY_ENV``, ``KEY_ENV_TO_PROVIDER``, the manifest
schema, curation, retirement marking, and the TUI's key-env maps — to model
something none of that machinery is for.

What ``mak/local`` does instead is ask **the running server**, live, every time.
A local model list is authoritative, instant, and changes the moment the user
pulls something, so there is no cache, no manifest, and nothing to retire.

It borrows exactly one thing from ``mak/models/``: the **fact / judgment split**.
Facts — which runtimes answered, which models are installed, how large a model's
context window is — are read from the server. Judgment — which models are worth
suggesting to someone who has none — lives in ``recommended.py``, a
hand-maintained table that nothing infers into.
"""

from mak.local.discovery import LOCAL_BASE_URL_ENV, discover, scan_targets
from mak.local.ollama_client import (
    DEFAULT_BASE_URL,
    OllamaChatResponse,
    OllamaClient,
    OllamaError,
    OllamaModel,
    PullProgress,
)
from mak.local.recommended import (
    RECOMMENDED,
    RecommendedModel,
    default_suggestion,
    recommended_for,
)
from mak.local.runtime import (
    KIND_OLLAMA,
    KIND_OPENAI_COMPATIBLE,
    LocalRuntime,
    probe_ollama,
    probe_openai_compatible,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "KIND_OLLAMA",
    "KIND_OPENAI_COMPATIBLE",
    "LOCAL_BASE_URL_ENV",
    "RECOMMENDED",
    "LocalRuntime",
    "OllamaChatResponse",
    "OllamaClient",
    "OllamaError",
    "OllamaModel",
    "PullProgress",
    "RecommendedModel",
    "default_suggestion",
    "discover",
    "probe_ollama",
    "probe_openai_compatible",
    "recommended_for",
    "scan_targets",
]
