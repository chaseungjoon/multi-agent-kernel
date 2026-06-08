"""Gemini API adapter — a first-party API backend via Google's GenAI SDK.

Like the Anthropic adapter, this talks to the model API directly and *forces*
structured output rather than scraping a CLI. Gemini's equivalent of a pinned
``tool_choice`` is a function-calling config in ``ANY`` mode restricted to a single
declared function (``submit_task_result``) whose parameter schema is the
``TaskResult`` shape — so the model must return a well-formed result object, not
prose.

Design note (why this is an *API* adapter, not an autonomous coding agent): MAK's
agent contract is a pure fragment transform (one node in → one node out as a strict
``TaskResult``); the kernel owns planning, locking, conflict detection,
reconstruction, and git. An autonomous multi-loop agent that edits files in its own
sandbox conflicts with MAK's node-store-as-source-of-truth model. A single
structured call is the right fit, so this adapter uses ``generate_content`` with
forced function calling, mirroring ``anthropic_api`` / ``openai_api``.

The SDK is imported lazily and the client is injectable, so neither the adapter nor
its tests require ``google-genai`` unless a real call is made. The API key is passed
in explicitly by the composition root (from ``GEMINI_API_KEY``), not read from the
SDK's own env defaults.
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

_DEFAULT_MODEL = "gemini-3-pro"
_RESULT_FN_NAME = "submit_task_result"

# Gemini function declaration. Schema is the OpenAPI subset Gemini accepts:
# `nullable` (not a JSON-Schema `type` union) marks the optional error field.
_RESULT_FUNCTION: dict[str, Any] = {
    "name": _RESULT_FN_NAME,
    "description": (
        "Report the structured outcome of the assigned MAK task. You MUST call "
        "this function exactly once as your final action."
    ),
    "parameters": {
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
                "type": "string",
                "nullable": True,
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
    f"calling the '{_RESULT_FN_NAME}' function. Echo back the same task_id. List "
    "every node id you changed in modified_nodes. Do not reply with prose."
)


class GeminiApiAdapter(AgentAdapter):
    """Gemini adapter: GenAI ``generate_content`` with forced function calling."""

    agent_type = "gemini_api"

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        api_key: str | None = None,
        agent_id: str = "gemini-0",
    ) -> None:
        self.agent_id = agent_id
        self.model = model
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        """Return the SDK client, constructing one lazily on first real use."""
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - exercised via health_check
                raise AgentError(
                    "google-genai SDK not installed; run `pip install google-genai`"
                ) from exc
            self._client = (
                genai.Client(api_key=self._api_key)
                if self._api_key is not None
                else genai.Client()
            )
        return self._client

    def format_task(self, task_bundle: TaskBundle) -> str:
        """Serialize the task bundle to the JSON sent as the user message."""
        return encode_task_bundle(task_bundle)

    def send(self, prompt: str) -> str:
        """Call ``generate_content`` with a forced function call; return its JSON."""
        client = self._get_client()
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={
                "system_instruction": _SYSTEM_PROMPT,
                "tools": [{"function_declarations": [_RESULT_FUNCTION]}],
                "tool_config": {
                    "function_calling_config": {
                        "mode": "ANY",
                        "allowed_function_names": [_RESULT_FN_NAME],
                    }
                },
            },
        )
        return self._extract_function_call(response)

    @staticmethod
    def _extract_function_call(response: Any) -> str:
        """Pull the ``submit_task_result`` function-call args out of the response."""
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                call = getattr(part, "function_call", None)
                if call is not None and getattr(call, "name", None) == _RESULT_FN_NAME:
                    payload = dict(call.args)
                    payload["protocol_version"] = PROTOCOL_VERSION
                    return json.dumps(payload)
        raise AgentError(
            f"gemini response contained no '{_RESULT_FN_NAME}' function call"
        )

    def parse_result(self, raw_output: str) -> TaskResult:
        """Decode the function-call payload into a ``TaskResult``."""
        return decode_task_result(raw_output)

    def health_check(self) -> bool:
        """Return whether an SDK client can be constructed (SDK present + key)."""
        try:
            self._get_client()
            return True
        except Exception:
            return False
