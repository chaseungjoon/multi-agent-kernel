"""Concrete ``PlannerLLM`` implementations backed by the model APIs.

The planner needs a plain prompt-in/text-out completion (it does its own JSON
parsing and validation), so these are thin wrappers over each SDK's basic call —
distinct from the agent adapters, which force a structured ``TaskResult``.

``build_planner_llm(model)`` picks the backend from the model id prefix. As with the
adapters, SDKs are imported lazily and clients are injectable, so constructing a
planner LLM needs no SDK installed and makes no network call until ``complete`` runs.
"""

from __future__ import annotations

from typing import Any

from mak.core.exceptions import PlannerFailedError
from mak.planner.planner import PlannerLLM

_DEFAULT_MAX_TOKENS = 4096


class AnthropicPlannerLLM:
    """Planner completion via the Anthropic Messages API."""

    def __init__(
        self,
        *,
        model: str,
        client: Any | None = None,
        api_key: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - exercised via build
                raise PlannerFailedError(
                    "anthropic SDK not installed; run `pip install anthropic`"
                ) from exc
            self._client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key is not None
                else anthropic.Anthropic()
            )
        return self._client

    def complete(self, prompt: str) -> str:
        """Return the model's text completion for ``prompt``."""
        response = self._get_client().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [
            block.text
            for block in getattr(response, "content", []) or []
            if getattr(block, "type", None) == "text"
        ]
        return "".join(parts)


class OpenAiPlannerLLM:
    """Planner completion via OpenAI Chat Completions."""

    def __init__(
        self,
        *,
        model: str,
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - exercised via build
                raise PlannerFailedError(
                    "openai SDK not installed; run `pip install openai`"
                ) from exc
            self._client = (
                openai.OpenAI(api_key=self._api_key)
                if self._api_key is not None
                else openai.OpenAI()
            )
        return self._client

    def complete(self, prompt: str) -> str:
        """Return the model's text completion for ``prompt``."""
        response = self._get_client().chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        return choices[0].message.content or ""


class GeminiPlannerLLM:
    """Planner completion via Google GenAI ``generate_content``."""

    def __init__(
        self,
        *,
        model: str,
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - exercised via build
                raise PlannerFailedError(
                    "google-genai SDK not installed; run `pip install google-genai`"
                ) from exc
            self._client = (
                genai.Client(api_key=self._api_key)
                if self._api_key is not None
                else genai.Client()
            )
        return self._client

    def complete(self, prompt: str) -> str:
        """Return the model's text completion for ``prompt``."""
        response = self._get_client().models.generate_content(
            model=self.model,
            contents=prompt,
        )
        return getattr(response, "text", None) or ""


def build_planner_llm(model: str, *, api_key: str | None = None) -> PlannerLLM:
    """Pick a ``PlannerLLM`` for ``model`` by its id prefix.

    ``claude*`` → Anthropic, ``gemini*`` → Gemini, ``gpt*``/``o1``/``o3``/``o4`` →
    OpenAI. Raises ``PlannerFailedError`` for an unrecognized model id.
    """
    lowered = model.lower()
    if lowered.startswith("claude"):
        return AnthropicPlannerLLM(model=model, api_key=api_key)
    if lowered.startswith("gemini"):
        return GeminiPlannerLLM(model=model, api_key=api_key)
    if lowered.startswith(("gpt", "o1", "o3", "o4")):
        return OpenAiPlannerLLM(model=model, api_key=api_key)
    raise PlannerFailedError(
        f"cannot infer a planner backend for model '{model}'; "
        "use a claude-*, gpt-*, or gemini-* model"
    )
