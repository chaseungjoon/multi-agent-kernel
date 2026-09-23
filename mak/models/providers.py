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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from mak.core.exceptions import MakError
from mak.endpoints.health import redact_secrets
from mak.endpoints.resolution import ResolvedEndpoint
from mak.endpoints.types import ModelDiscovery, Transport

DEFAULT_TIMEOUT = 10.0


class ModelFetchError(MakError):
    """A provider's model list could not be retrieved."""


@dataclass(frozen=True, slots=True)
class FetchedModel:
    """Raw facts about one model, as reported by its provider.

    ``supported_parameters`` is **tri-state**, and the distinction is
    load-bearing (Wave 24):

    * ``None`` — this provider published no capability metadata. Anthropic,
      OpenAI and Gemini never do, and most OpenAI-compatible ``/models``
      implementations return bare ids. Runtime negotiation is the only source
      of truth there, so nothing is assumed.
    * ``frozenset()`` — the field was published and was empty.
    * a non-empty set — the parameters reported for this **exact** model id.

    A boolean would conflate "known unsupported" with "unknown" and would
    silently disable structured output for every endpoint that lists ids only.
    """

    model_id: str
    display_name: str = ""
    context_window: int | None = None
    max_output: int | None = None
    supported_parameters: frozenset[str] | None = None


class ModelSource(Protocol):
    """Fetches the currently-offered model list for one provider."""

    provider: str

    def fetch(
        self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> list[FetchedModel]:
        """Return the provider's current models, or raise ``ModelFetchError``."""
        ...


def reported_parameters(model: Any) -> frozenset[str] | None:
    """Return a model's reported request parameters, or None if it reported none.

    The one place any SDK-specific knowledge of this field lives. OpenRouter
    publishes ``supported_parameters`` on every ``/models`` row, and the openai
    SDK — which types ``Model`` with four fields and none of them this one —
    keeps unknown keys as pydantic extras. Verified against openai 3.16.2:
    the value is reachable both through ``model_extra`` and as an attribute.

    ``model_extra`` is preferred because it is unambiguous: were a future SDK
    to add a real field of this name with a different type, reading extras
    keeps MAK looking at the server's own value.

    Anything that is not a list of strings returns ``None`` — "the service did
    not report this" — rather than an empty set, because an empty set is itself
    a meaningful report (see :class:`FetchedModel`).
    """
    extra = getattr(model, "model_extra", None)
    raw: Any = None
    if isinstance(extra, dict) and "supported_parameters" in extra:
        raw = extra["supported_parameters"]
    else:
        raw = getattr(model, "supported_parameters", None)
    if raw is None or isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        return None
    return frozenset(str(item) for item in raw if isinstance(item, str))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# Provider -> the packaging extra that installs its SDK. Named in the failure
# message because "No module named 'anthropic'" tells a user what is missing but
# not what to type, and MAK's SDKs are optional extras as of Wave 15.
_SDK_EXTRA: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "gemini",
}


def _missing_sdk(provider: str, exc: ImportError) -> ModelFetchError:
    """Return the fetch failure for a provider whose SDK is not installed."""
    extra = _SDK_EXTRA[provider]
    return ModelFetchError(
        f"{provider}: SDK not installed ({exc}); run "
        f"'pip install \"multi-agent-kernel[{extra}]\"'"
    )


class AnthropicSource:
    """Anthropic ``/v1/models`` — the richest of the three (limits included)."""

    provider = "anthropic"

    def fetch(
        self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> list[FetchedModel]:
        """Fetch Anthropic's model list (the SDK paginates on iteration)."""
        try:
            import anthropic
        except ImportError as exc:
            raise _missing_sdk("anthropic", exc) from exc
        try:
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
        except ImportError as exc:
            raise _missing_sdk("openai", exc) from exc
        try:
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
            from google.genai import types
        except ImportError as exc:
            raise _missing_sdk("gemini", exc) from exc
        try:
            # google-genai has no default timeout; without one a stalled
            # connection blocks /refresh-models indefinitely. Milliseconds.
            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(timeout * 1000)),
            )
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


class OpenAiCompatibleSource:
    """``GET /models`` on any endpoint speaking the OpenAI protocol.

    Distinct from :class:`OpenAiSource` in one way that matters: it is built
    from a **resolved endpoint** rather than hard-coded to OpenAI's own host, so
    NVIDIA, OpenRouter, DeepSeek, Z.ai, vLLM and llama.cpp all list through it
    with no new code. ``provider`` is the endpoint id, because that is what the
    catalog keys on.

    The credential is taken from the endpoint, never from the ambient
    environment: this is the model-listing half of the same rule the agent
    adapter enforces for dispatch, and forgetting it here would send a real
    OpenAI key to a third-party host just to ask what models it has.
    """

    def __init__(self, endpoint: ResolvedEndpoint) -> None:
        self.provider = endpoint.id
        self._endpoint = endpoint

    def fetch(
        self, api_key: str, *, timeout: float = DEFAULT_TIMEOUT
    ) -> list[FetchedModel]:
        """Fetch the endpoint's model list (ids; most services expose no limits)."""
        try:
            import openai
        except ImportError as exc:
            raise _missing_sdk("openai", exc) from exc
        options: dict[str, Any] = {
            # Always explicit, never left for the SDK to resolve.
            "api_key": api_key or self._endpoint.effective_key() or "local",
            "timeout": timeout,
        }
        if self._endpoint.base_url is not None:
            options["base_url"] = self._endpoint.base_url
        if self._endpoint.headers:
            options["default_headers"] = dict(self._endpoint.headers)
        try:
            client = openai.OpenAI(**options)
            return [
                FetchedModel(
                    model_id=str(m.id),
                    # A few compatible services do return a friendly name; most
                    # do not, and the id is then the honest label.
                    display_name=str(getattr(m, "name", "") or ""),
                    # Capability metadata was previously discarded here, which
                    # is why MAK could only learn "this model refuses schemas"
                    # by being refused. OpenRouter has published it all along.
                    supported_parameters=reported_parameters(m),
                )
                for m in client.models.list()
                if getattr(m, "id", None)
            ]
        except Exception as exc:  # noqa: BLE001 - any SDK failure is a fetch failure
            raise ModelFetchError(
                f"{self.provider}: {redact_secrets(str(exc))}"
            ) from exc


def default_sources() -> tuple[ModelSource, ...]:
    """Return one source per built-in provider, in display order."""
    return (AnthropicSource(), OpenAiSource(), GeminiSource())


def sources_for_endpoints(
    endpoints: Sequence[ResolvedEndpoint],
) -> tuple[ModelSource, ...]:
    """Return a source for each endpoint whose discovery policy allows listing.

    ``manual`` endpoints are excluded entirely rather than included and skipped,
    so "this endpoint makes no network call" is visible in the source list
    itself instead of buried in a branch.
    """
    return tuple(
        OpenAiCompatibleSource(endpoint)
        for endpoint in endpoints
        if endpoint.transport is Transport.OPENAI_CHAT
        and endpoint.model_discovery is not ModelDiscovery.MANUAL
    )
