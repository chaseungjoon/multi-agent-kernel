"""Read a provider's stop signal, and carry it through to the ``TaskResult``.

Every provider says why it stopped generating — Anthropic's ``stop_reason``,
OpenAI's ``finish_reason``, Gemini's ``candidate.finish_reason`` — and MAK read
none of them. That single omission is what let a reply cut off at the output cap
arrive as a successful, empty ``TaskResult``: the truncated payload and a
deliberate "nothing to change" are byte-identical once the stop signal is thrown
away.

Two jobs live here, both shared by the three API adapters so the contract is
uniform:

- :func:`check_stop_reason` fails *loudly and typed* on a cut or a refusal,
  before any caller can read the partial payload as a result;
- :func:`with_response_metadata` and :func:`extract_usage` attach the stop reason
  and the provider's token counts to the payload the protocol decodes, so
  ``AGENT_RESULT`` records them for a *good* attempt too and the budget question
  becomes measurable instead of argued.
"""

from __future__ import annotations

import json
from typing import Any

from mak.agent_runner.adapters.budget import (
    REFUSAL_STOP_REASONS,
    TRUNCATION_STOP_REASONS,
)
from mak.core.exceptions import AgentRefusedError, AgentTruncatedError

# The SDKs hand back an enum whose ``str`` is e.g. ``FinishReason.MAX_TOKENS``,
# while older versions and the REST shape use a plain string — so signals are
# matched on the text, as the planner's Gemini backend already does.
_USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "total_tokens": "total_tokens",
    "prompt_token_count": "input_tokens",
    "candidates_token_count": "output_tokens",
    "total_token_count": "total_tokens",
}


def matches(stop_reason: object, signals: frozenset[str]) -> bool:
    """Whether ``stop_reason`` names one of ``signals`` (substring, case-aware)."""
    if stop_reason is None:
        return False
    text = str(stop_reason)
    return any(signal in text for signal in signals)


def check_stop_reason(
    stop_reason: object,
    *,
    provider: str,
    budget: int | None = None,
    usage: dict[str, int] | None = None,
) -> None:
    """Raise if the provider signalled a cut or a refusal; return otherwise.

    ``AgentTruncatedError`` is retryable (with a note asking for less, not the
    identical request); ``AgentRefusedError`` is not — the same prompt earns the
    same refusal while burning the attempt budget.
    """
    if matches(stop_reason, TRUNCATION_STOP_REASONS):
        limit = f"{budget}-token " if budget is not None else ""
        raise AgentTruncatedError(
            f"{provider} stopped at the {limit}output limit before the result was "
            "complete; the returned fragments are incomplete and were discarded",
            stop_reason=str(stop_reason),
            usage=usage,
        )
    if matches(stop_reason, REFUSAL_STOP_REASONS):
        raise AgentRefusedError(
            f"{provider} declined to produce a result (stop reason: "
            f"{stop_reason}); retrying the same task will not change that",
            stop_reason=str(stop_reason),
            usage=usage,
        )


def extract_usage(usage: object) -> dict[str, int]:
    """Normalize a provider usage object into ``{input,output,total}_tokens``.

    Providers name the same three numbers differently; unifying them here is what
    lets one ``AGENT_RESULT`` field mean the same thing whichever backend ran.
    """
    if usage is None:
        return {}
    counts: dict[str, int] = {}
    for source_name, canonical in _USAGE_FIELDS.items():
        value = getattr(usage, source_name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(source_name)
        if isinstance(value, int) and not isinstance(value, bool):
            counts[canonical] = value
    return counts


def with_response_metadata(
    payload: str, *, stop_reason: object, usage: dict[str, int]
) -> str:
    """Return ``payload`` with the provider's stop reason and usage merged in.

    The adapter's values are written *after* the model's own keys, so a model
    that happens to emit a field of the same name cannot forge its own telemetry.
    """
    data = json.loads(payload)
    if not isinstance(data, dict):
        return payload
    merged: dict[str, Any] = dict(data)
    if stop_reason is not None:
        merged["stop_reason"] = str(stop_reason)
    if usage:
        merged["usage"] = usage
    return json.dumps(merged)
