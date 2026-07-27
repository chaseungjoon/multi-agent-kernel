"""Concrete ``PlannerLLM`` implementations backed by the model APIs.

The planner needs a plain prompt-in/text-out completion (it does its own JSON
parsing and validation), so these are thin wrappers over each SDK's basic call —
distinct from the agent adapters, which force a structured ``TaskResult``.

``build_planner_llm(model)`` picks the backend from the model id prefix. As with the
adapters, SDKs are imported lazily and clients are injectable, so constructing a
planner LLM needs no SDK installed and makes no network call until ``complete`` runs.

**Output budget.** A plan for a real repository runs to thousands of tokens, and a
budget too small to hold it truncates the JSON mid-string — a failure that then
repeats on every retry, because the same request produces the same over-long plan
and the same cut. The budget is therefore taken from the model's own documented
output limit (via the model catalog) rather than from one fixed number, and every
backend reports a provider-signalled cut as ``TruncatedResponseError`` so the
planner can ask for a smaller plan instead of blindly retrying.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from mak.core.exceptions import PlannerFailedError
from mak.planner.planner import PlannerLLM
from mak.planner.response import ResponseError, TruncatedResponseError

# Used when the catalog knows nothing about the model. Twice the agent adapters'
# budget: a plan spans the whole repo, while an agent result covers a few nodes.
_DEFAULT_MAX_TOKENS = 16384
# Floor and ceiling around whatever the catalog reports. The ceiling keeps the
# request comfortably inside the non-streaming window the SDKs allow; a plan that
# genuinely needs more than this is one the planner should be asked to compact.
_MIN_MAX_TOKENS = 4096
_MAX_MAX_TOKENS = 32000


@lru_cache(maxsize=1)
def _documented_output_limits() -> dict[str, int]:
    """Return ``{model_id: max_output}`` from the model catalog."""
    try:
        from mak.models.registry import ModelRegistry

        return {
            entry.model_id: entry.max_output
            for entry in ModelRegistry().all_models()
            if entry.max_output
        }
    except Exception:  # noqa: BLE001 - the budget below is a safe fallback
        # The catalog is an optimisation, not a dependency: a missing manifest or
        # an unreadable cache must not stop the planner from running.
        return {}


def resolve_max_tokens(model: str) -> int:
    """Return the output-token budget to request for ``model``.

    Uses the model's documented output limit when the catalog knows it, clamped
    to a sane range; falls back to ``_DEFAULT_MAX_TOKENS`` for an unknown model.
    """
    documented = _documented_output_limits().get(model)
    if documented is None:
        return _DEFAULT_MAX_TOKENS
    return max(_MIN_MAX_TOKENS, min(documented, _MAX_MAX_TOKENS))


class AnthropicPlannerLLM:
    """Planner completion via the Anthropic Messages API."""

    def __init__(
        self,
        *,
        model: str,
        client: Any | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = (
            max_tokens if max_tokens is not None else resolve_max_tokens(model)
        )
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
        """Return the model's text completion for ``prompt``.

        Raises ``TruncatedResponseError`` when the reply hit the output cap, so
        the planner retries with a compaction instruction instead of re-issuing
        an identical request that would be cut at the identical point.
        """
        response = self._get_client().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "max_tokens":
            raise TruncatedResponseError(
                f"anthropic stopped at the {self.max_tokens}-token output limit "
                "before the plan was complete"
            )
        if stop_reason == "refusal":
            # Not retryable: the model declined, and re-sending the same prompt
            # gets the same refusal while burning the budget.
            raise PlannerFailedError(
                f"the planner model '{self.model}' declined to produce a plan "
                "(refusal stop reason); rephrase the task or use another model"
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
        choice = choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise TruncatedResponseError(
                "openai stopped at the model's output-token limit before the "
                "plan was complete"
            )
        return choice.message.content or ""


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
        reason = _gemini_finish_reason(response)
        if "MAX_TOKENS" in reason:
            raise TruncatedResponseError(
                "gemini stopped at the model's output-token limit before the "
                "plan was complete"
            )
        text = getattr(response, "text", None) or ""
        if not text and reason:
            # An empty candidate carries its reason only here (safety, recitation);
            # surfacing it beats reporting a bare "empty response".
            raise ResponseError(f"gemini returned no text (finish reason: {reason})")
        return text


def _gemini_finish_reason(response: Any) -> str:
    """Return the first candidate's finish reason as a string ("" when absent).

    The SDK hands back an enum whose ``str`` is ``FinishReason.MAX_TOKENS``, but
    older versions and the REST shape use a plain string, so compare on the text.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    reason = getattr(candidates[0], "finish_reason", None)
    return "" if reason is None else str(reason)


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
