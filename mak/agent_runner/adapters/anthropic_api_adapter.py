"""Anthropic API adapter — MAK's PRIMARY agent backend (funding condition #2).

This adapter talks to the Anthropic Messages API directly via the official SDK
rather than scraping a CLI's stdout (PLANS.md §6). Structured output is *forced*:
the request pins ``tool_choice`` to a single ``submit_task_result`` tool whose
``input_schema`` is exactly the ``TaskResult`` shape, so the model cannot reply
with prose — it must return a well-formed result object. No stdout parsing, no
regex, no format drift.

The SDK is imported lazily and the client is injectable, so the adapter (and its
tests) do not require the ``anthropic`` package to be installed unless a real call
is made.
"""

from __future__ import annotations

import json
from typing import Any

from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.agent_runner.protocol import (
    PROTOCOL_VERSION,
    decode_task_result,
    encode_task_bundle,
)
from mak.core.exceptions import AgentError
from mak.core.types import TaskBundle, TaskResult

_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_MAX_TOKENS = 4096
_RESULT_TOOL_NAME = "submit_task_result"

_RESULT_TOOL: dict[str, Any] = {
    "name": _RESULT_TOOL_NAME,
    "description": (
        "Report the structured outcome of the assigned MAK task. You MUST call "
        "this tool exactly once as your final action."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task_id from the received task bundle.",
            },
            "success": {
                "type": "boolean",
                "description": "True if the task was completed successfully.",
            },
            "modified_nodes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Node ids whose source this task modified.",
            },
            "error": {
                "type": ["string", "null"],
                "description": "Failure reason when success is false, else null.",
            },
        },
        "required": ["task_id", "success"],
    },
}

_SYSTEM_PROMPT = (
    "You are a MAK coding agent. You receive a single task as a JSON 'task "
    "bundle' describing a task_id, a description, the node ids you may modify, "
    "and read-only context. Carry out the task, then report the outcome by "
    f"calling the '{_RESULT_TOOL_NAME}' tool. Echo back the same task_id. List "
    "every node id you changed in modified_nodes. Do not reply with prose."
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
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        agent_id: str = "anthropic-0",
    ) -> None:
        self.agent_id = agent_id
        self.model = model
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        """Return the SDK client, constructing one lazily on first real use."""
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - exercised via health_check
                raise AgentError(
                    "anthropic SDK not installed; run `pip install anthropic`"
                ) from exc
            self._client = (
                anthropic.Anthropic(api_key=self._api_key)
                if self._api_key is not None
                else anthropic.Anthropic()
            )
        return self._client

    def format_task(self, task_bundle: TaskBundle) -> str:
        """Serialize the task bundle to the JSON sent as the user message."""
        return encode_task_bundle(task_bundle)

    def send(self, prompt: str) -> str:
        """Call the Messages API and return the result tool's JSON payload."""
        client = self._get_client()
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_SYSTEM_PROMPT,
            tools=[_RESULT_TOOL],
            tool_choice={"type": "tool", "name": _RESULT_TOOL_NAME},
            messages=[{"role": "user", "content": prompt}],
        )
        return self._extract_tool_payload(response)

    @staticmethod
    def _extract_tool_payload(response: Any) -> str:
        """Pull the ``submit_task_result`` tool_use input out of the response."""
        for block in getattr(response, "content", []) or []:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == _RESULT_TOOL_NAME
            ):
                payload = dict(block.input)
                payload["protocol_version"] = PROTOCOL_VERSION
                return json.dumps(payload)
        raise AgentError(
            f"anthropic response contained no '{_RESULT_TOOL_NAME}' tool_use block"
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
