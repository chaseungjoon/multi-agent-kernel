"""One short follow-up turn when a reply arrives but cannot be decoded.

A decode failure used to cost a full re-dispatch: the session fails the attempt
and the *whole* bundle goes out again — write sources, sibling context, caller
context, tens of KB — to re-earn an answer the model had already worked out and
merely mis-shaped. For a frontier model that is a rare tax. For a small local
model a malformed first reply is the common case, which is what makes this loop
worth having rather than a micro-optimisation.

The loop is shared by every adapter that needs it, parameterized by four
callables, so the OpenAI-compatible and native-Ollama implementations cannot
drift into repairing differently:

``call``      — make one request from the current message list.
``read_meta`` — return ``(usage, stop_reason, raw_text)`` for a response, and
                raise for a cut or a refusal *before* any payload is read.
``extract``   — return the response's JSON payload, or raise
                ``AgentProtocolError`` describing what was wrong with it.
``follow_up`` — append the model's own previous reply plus the repair
                instruction, and return the new message list.

Three properties matter, each for a reason:

- **A truncation or a refusal is never repaired.** Both repeat verbatim on the
  same request, so a repair turn would spend tokens to be told the same thing.
  That is why ``read_meta`` runs first and is outside the repairable block —
  the same ordering ``check_stop_reason`` already imposes on every backend.
- **Usage is summed across turns.** ``Session.total_tokens`` — and therefore
  ``session.max_total_tokens``, the only spend ceiling MAK has — is computed
  from what the adapter reports. A repair turn billing invisibly would put that
  ceiling out by however many repairs a run needed.
- **A failure carries the summed usage too**, so an attempt that exhausted its
  repairs is as accountable in the log as one that succeeded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mak.agent_runner.protocol import REPAIR_INSTRUCTION, decode_task_result
from mak.agent_runner.stop_signals import with_response_metadata
from mak.core.exceptions import AgentProtocolError, AgentResponseError

# One message list, in whatever dict shape the backend's chat API takes. Both
# transports this serves speak the OpenAI-style ``{"role", "content"}`` message.
Messages = list[dict[str, Any]]

# ``(usage, stop_reason, raw_reply_text)``. ``raw_reply_text`` is what goes back
# to the model as its own previous turn: it has to see what it actually said in
# order to fix the shape of it.
ResponseMeta = tuple[dict[str, int], object, str]


def _sum_usage(total: dict[str, int], turn: dict[str, int]) -> None:
    """Accumulate one turn's token counts into the running total, in place."""
    for key, value in turn.items():
        total[key] = total.get(key, 0) + value


def repair_loop(
    messages: Messages,
    *,
    call: Callable[[Messages], Any],
    read_meta: Callable[[Any], ResponseMeta],
    extract: Callable[[Any], str],
    follow_up: Callable[[Messages, str, str], Messages],
    repair_attempts: int,
) -> str:
    """Call the backend, repairing a malformed reply up to ``repair_attempts``.

    Returns the decodable payload with the stop reason, the **summed** usage, and
    the repair count merged in. Re-raises a cut or a refusal untouched (both
    repeat verbatim, so neither is repairable), and raises the last
    ``AgentProtocolError`` when the repair budget is exhausted.
    """
    usage_total: dict[str, int] = {}
    for attempt in range(repair_attempts + 1):
        response = call(messages)
        try:
            usage, stop_reason, raw_text = read_meta(response)
        except AgentResponseError as exc:
            # A cut or a refusal: not repairable, but its turn still cost
            # tokens, and so did every turn before it.
            _sum_usage(usage_total, exc.usage)
            exc.usage = dict(usage_total)
            raise
        _sum_usage(usage_total, usage)
        try:
            payload = extract(response)
            # A *validation* decode: its result is thrown away and
            # ``parse_result`` decodes the payload again downstream. Decoding
            # twice is cheap next to a model call, and it keeps ``parse_result``
            # a pure function of the string the adapter returns.
            decode_task_result(payload)
        except AgentProtocolError as exc:
            if attempt == repair_attempts:
                exc.usage = dict(usage_total)
                if exc.stop_reason is None and stop_reason is not None:
                    exc.stop_reason = str(stop_reason)
                raise
            messages = follow_up(
                messages, raw_text, REPAIR_INSTRUCTION.format(reason=exc)
            )
            continue
        return with_response_metadata(
            payload,
            stop_reason=stop_reason,
            usage=dict(usage_total),
            repairs=attempt,
        )
    # Unreachable: ``repair_attempts`` is non-negative, so the loop always runs
    # at least once and its last iteration either returns or raises.
    raise AgentProtocolError("repair loop ended without a result")
