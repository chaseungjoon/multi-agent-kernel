"""Anthropic API adapter — MAK's PRIMARY agent backend.

This adapter talks to the Anthropic Messages API directly via the official SDK
rather than scraping a CLI's stdout. Structured output is *forced*:
the request pins ``tool_choice`` to a single ``submit_task_result`` tool whose
``input_schema`` is exactly the ``TaskResult`` shape, so the model cannot reply
with prose — it must return a well-formed result object. No stdout parsing, no
regex, no format drift.

**Output budget.** A forced tool call carries the node's whole rewritten source
in one response, so a budget below the file's size cuts the ``modified_fragments``
array mid-generation. What arrives is a ``tool_use`` block holding only the scalar
fields that finished — ``{task_id, success}`` — which decodes into a perfectly
valid *successful* result with no changes: work reported as done that was never
done. The budget is therefore taken from the model's documented output limit, and
``stop_reason`` is checked **before** the payload is read, so a cut can never be
mistaken for "nothing to change". The request streams for the same reason the
planner's does: at a real budget the SDK refuses a non-streaming call.

The SDK is imported lazily and the client is injectable, so the adapter (and its
tests) do not require the ``anthropic`` package to be installed unless a real call
is made.
"""

from __future__ import annotations

import json
from typing import Any

from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.agent_runner.adapters.budget import resolve_agent_max_tokens
from mak.agent_runner.adapters.result_schema import (
    RESULT_TOOL_DESCRIPTION,
    RESULT_TOOL_NAME,
    result_schema,
)
from mak.agent_runner.protocol import (
    NO_CHANGE_CONTRACT,
    NODE_ID_CONTRACT,
    PROTOCOL_VERSION,
    RETRY_NOTE_CONTRACT,
    decode_task_result,
    encode_task_bundle,
)
from mak.agent_runner.stop_signals import (
    check_stop_reason,
    extract_usage,
    with_response_metadata,
)
from mak.core.exceptions import AgentError, AgentProtocolError
from mak.core.types import TaskBundle, TaskResult

_DEFAULT_MODEL = "claude-sonnet-5"
_RESULT_TOOL_NAME = RESULT_TOOL_NAME

# The one TaskResult contract, rendered in Anthropic's JSON-Schema dialect. It
# lives in ``result_schema`` rather than here so the four backends that must
# describe the same five keys cannot drift apart.
_RESULT_TOOL: dict[str, Any] = {
    "name": _RESULT_TOOL_NAME,
    "description": RESULT_TOOL_DESCRIPTION.format(noun="tool"),
    "input_schema": result_schema("anthropic"),
}

_SYSTEM_PROMPT = (
    "You are a MAK coding agent. You receive a single task as a JSON 'task "
    "bundle' describing a task_id, a description, the node ids you may modify, "
    "and read-only context (the current source of each node is in 'context' "
    "under 'write_source:<id>' / 'read_source:<id>'). Carry out the task, then "
    f"report the outcome by calling the '{_RESULT_TOOL_NAME}' tool. Echo back the "
    "same task_id. For every node you changed, put its id and its FULL rewritten "
    "source in 'modified_fragments' — return complete node source, never a diff, "
    "and only for nodes you were authorized to modify. "
    f"{NODE_ID_CONTRACT} {NO_CHANGE_CONTRACT} {RETRY_NOTE_CONTRACT} "
    "Do not reply with prose."
)


class AnthropicApiAdapter(AgentAdapter):
    """Primary adapter: Anthropic Messages API with forced structured output."""

    agent_type = "anthropic_api"

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        api_key: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        agent_id: str = "anthropic-0",
    ) -> None:
        self.agent_id = agent_id
        self.model = model
        # None means "ask the catalog what this model can actually emit" — a
        # constant here is what silently clipped every large file.
        self.max_tokens = (
            max_tokens if max_tokens is not None else resolve_agent_max_tokens(model)
        )
        # Seconds. None leaves the SDK's own default in place. Without a bound a
        # wedged call never returns, and the session's collect timeout cannot
        # help: it stops waiting, then blocks forever shutting the pool down.
        self.timeout = timeout
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        """Return the SDK client, constructing one lazily on first real use."""
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - exercised via health_check
                raise AgentError(
                    "anthropic SDK not installed; run "
                    "'pip install \"multi-agent-kernel[anthropic]\"'"
                ) from exc
            options: dict[str, Any] = {}
            if self._api_key is not None:
                options["api_key"] = self._api_key
            if self.timeout is not None:
                options["timeout"] = self.timeout
            self._client = anthropic.Anthropic(**options)
        return self._client

    def format_task(self, task_bundle: TaskBundle) -> str:
        """Serialize the task bundle to the JSON sent as the user message."""
        return encode_task_bundle(task_bundle)

    def send(self, prompt: str) -> str:
        """Call the Messages API and return the result tool's JSON payload.

        Streams the request: an agent-sized output budget is large enough that
        the SDK rejects a plain ``messages.create`` ("Streaming is required for
        operations that may take longer than 10 minutes").
        ``get_final_message`` yields the same assembled message a non-streaming
        call would have returned, ``tool_use`` block included.
        """
        client = self._get_client()
        with client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_SYSTEM_PROMPT,
            tools=[_RESULT_TOOL],
            tool_choice={"type": "tool", "name": _RESULT_TOOL_NAME},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
        return self._read_response(response)

    def _read_response(self, response: Any) -> str:
        """Reject a cut or refused reply, then extract the result tool payload.

        Order matters: on a cut, ``block.input`` holds whatever the model had
        finished writing, which for a partial array is a syntactically fine
        result object missing the work. Checking the stop reason first is what
        keeps that from being read as a success.
        """
        usage = extract_usage(getattr(response, "usage", None))
        stop_reason = getattr(response, "stop_reason", None)
        check_stop_reason(
            stop_reason,
            provider="anthropic",
            budget=self.max_tokens,
            usage=usage,
        )
        payload = self._extract_tool_payload(
            response, stop_reason=stop_reason, usage=usage
        )
        return with_response_metadata(payload, stop_reason=stop_reason, usage=usage)

    @staticmethod
    def _extract_tool_payload(
        response: Any,
        *,
        stop_reason: object = None,
        usage: dict[str, int] | None = None,
    ) -> str:
        """Pull the ``submit_task_result`` tool_use input out of the response.

        A reply with no result block is an ``AgentProtocolError``, not a bare
        ``AgentError``: the call succeeded and the model answered, just not in
        the shape the protocol can decode. Only that classification reaches the
        session's schema-restating retry note — reported as a transport failure
        it drew the generic "produced nothing usable" note instead. The
        provider's own signals travel with it so a rejected reply stays
        accountable in the log.
        """
        for block in getattr(response, "content", []) or []:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == _RESULT_TOOL_NAME
            ):
                payload = dict(block.input)
                payload["protocol_version"] = PROTOCOL_VERSION
                return json.dumps(payload)
        raise AgentProtocolError(
            f"anthropic response contained no '{_RESULT_TOOL_NAME}' tool_use block",
            stop_reason=None if stop_reason is None else str(stop_reason),
            usage=usage,
        )

    def parse_result(self, raw_output: str) -> TaskResult:
        """Decode the tool payload into a ``TaskResult``."""
        return decode_task_result(raw_output)

    def health_check(self) -> bool:
        """Return whether an SDK client can be constructed (SDK present + key)."""
        try:
            self._get_client()
            return True
        except Exception:
            return False
