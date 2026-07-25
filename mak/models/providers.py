"""Provider model-list fetchers — the only network-facing code in the subsystem.

Each source wraps one vendor SDK's list-models endpoint and returns plain
``FetchedModel`` records. SDK imports are **lazy** (inside ``fetch``) so importing
this module costs nothing and a missing optional SDK never breaks startup — the
same discipline as ``mak/agent_runner/adapters/*``.

Every fetcher converts *any* SDK exception into ``ModelFetchError``. A provider
outage, an expired key, or a malformed response must degrade to "keep the cached
list", never propagate.

Field names below are taken from the installed SDKs, not guessed:

* ``anthropic``: ``id``, ``display_name``, ``max_input_tokens``, ``max_tokens``
* ``openai``:    ``id``, ``created``, ``owned_by`` — **no** display name or limits
* ``google-genai``: ``name`` (``models/``-prefixed), ``display_name``,
  ``input_token_limit``, ``output_token_limit``, ``supported_actions``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from mak.core.exceptions import MakError

DEFAULT_TIMEOUT = 10.0


class ModelFetchError(MakError):
    """A provider's model list could not be retrieved."""


@dataclass(frozen=True, slots=True)
class FetchedModel:
    """Raw facts about one model, as reported by its provider."""

    model_id: str
    display_name: str = ""
    context_window: int | None = None
    max_output: int | None = None


class ModelSource(Protocol):
    """Fetches the currently-offered model list for one provider."""

    provider: str

    def fetch(
        self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> list[FetchedModel]:
        """Return the provider's current models, or raise ``ModelFetchError``."""
        ...


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class AnthropicSource:
    """Anthropic ``/v1/models`` — the richest of the three (limits included)."""

    provider = "anthropic"

    def fetch(
        self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> list[FetchedModel]:
        """Fetch Anthropic's model list (the SDK paginates on iteration)."""
        try:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
            return [
                FetchedModel(
                    model_id=str(m.id),
                    display_name=str(getattr(m, "display_name", "") or ""),
                    context_window=_int_or_none(
                        getattr(m, "max_input_tokens", None)
                    ),
                    max_output=_int_or_none(getattr(m, "max_tokens", None)),
                )
                for m in client.models.list()
                if getattr(m, "id", None)
            ]
        except Exception as exc:  # noqa: BLE001 - any SDK failure is a fetch failure
            raise ModelFetchError(f"anthropic: {exc}") from exc


class OpenAiSource:
    """OpenAI ``/v1/models`` — returns ids only, mixed with non-chat endpoints.

    There is no capability or context-window metadata to work with, so every
    non-chat endpoint is excluded downstream by ``curation.filter_ids``.
    """

    provider = "openai"

    def fetch(
        self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> list[FetchedModel]:
        """Fetch OpenAI's model list (bare ids; no limits are exposed)."""
        try:
            import openai

            client = openai.OpenAI(api_key=api_key, timeout=timeout)
            return [
                FetchedModel(model_id=str(m.id))
                for m in client.models.list()
                if getattr(m, "id", None)
            ]
        except Exception as exc:  # noqa: BLE001 - any SDK failure is a fetch failure
            raise ModelFetchError(f"openai: {exc}") from exc


class GeminiSource:
    """Google Gemini ``models.list`` — filtered to generateContent models."""

    provider = "gemini"

    def fetch(
        self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> list[FetchedModel]:
        """Fetch Gemini models that actually support content generation."""
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            models: list[FetchedModel] = []
            for m in client.models.list():
                name = str(getattr(m, "name", "") or "")
                if not name:
                    continue
                actions = getattr(m, "supported_actions", None) or ()
                # Embedding- and tuning-only endpoints are excluded at the source.
                if "generateContent" not in actions:
                    continue
                models.append(
                    FetchedModel(
                        model_id=name.removeprefix("models/"),
                        display_name=str(getattr(m, "display_name", "") or ""),
                        context_window=_int_or_none(
                            getattr(m, "input_token_limit", None)
                        ),
                        max_output=_int_or_none(
                            getattr(m, "output_token_limit", None)
                        ),
                    )
                )
            return models
        except Exception as exc:  # noqa: BLE001 - any SDK failure is a fetch failure
            raise ModelFetchError(f"gemini: {exc}") from exc


def default_sources() -> tuple[ModelSource, ...]:
    """Return one source per supported provider, in display order."""
    return (AnthropicSource(), OpenAiSource(), GeminiSource())
