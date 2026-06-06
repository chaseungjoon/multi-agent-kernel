"""OpenAI API adapter — a secondary first-party API backend (PLANS.md §6).

Like the Anthropic adapter, this talks to the API directly and forces structured
output, here via OpenAI's JSON mode (``response_format={"type": "json_object"}``).
The model is instructed to emit exactly the ``TaskResult`` field set, which is then
decoded through MAK's wire protocol — no stdout scraping.

The SDK is imported lazily and the client is injectable, so neither the adapter
nor its tests require the ``openai`` package unless a real call is made.
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

_DEFAULT_MODEL = "gpt-4o"

_SYSTEM_PROMPT = (
    "You are a MAK coding agent. You receive a single task as a JSON 'task "
    "bundle' (task_id, description, the node ids you may modify, and read-only "
    "context). Carry out the task, then respond with a JSON object containing "
    "exactly these keys: 'task_id' (string, echoing the bundle's task_id), "
    "'success' (boolean), 'modified_nodes' (array of node id strings you "
    "changed), and 'error' (string reason when success is false, otherwise "
    "null). Respond with only that JSON object."
)


class OpenAiApiAdapter(AgentAdapter):
    """OpenAI Chat Completions adapter using JSON mode for structured output."""

    agent_type = "openai_api"

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        api_key: str | None = None,
        agent_id: str = "openai-0",
    ) -> None:
        self.agent_id = agent_id
        self.model = model
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        """Return the SDK client, constructing one lazily on first real use."""
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - exercised via health_check
                raise AgentError(
                    "openai SDK not installed; run `pip install openai`"
                ) from exc
            self._client = (
                openai.OpenAI(api_key=self._api_key)
                if self._api_key is not None
                else openai.OpenAI()
            )
        return self._client

    def format_task(self, task_bundle: TaskBundle) -> str:
        """Serialize the task bundle to the JSON sent as the user message."""
        return encode_task_bundle(task_bundle)

    def send(self, prompt: str) -> str:
        """Call Chat Completions in JSON mode and return the result JSON."""
        client = self._get_client()
        response = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return self._extract_content(response)

    @staticmethod
    def _extract_content(response: Any) -> str:
        """Pull the JSON content out of the first choice and normalize it."""
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise AgentError("openai response contained no choices")
        content = choices[0].message.content
        if content is None:
            raise AgentError("openai response message had no content")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AgentError(f"openai response was not valid JSON: {exc}") from exc
        payload["protocol_version"] = PROTOCOL_VERSION
        return json.dumps(payload)

    def parse_result(self, raw_output: str) -> TaskResult:
        """Decode the JSON payload into a ``TaskResult``."""
        return decode_task_result(raw_output)

    def health_check(self) -> bool:
        """Return whether an SDK client can be constructed (SDK present + key)."""
        try:
            self._get_client()
            return True
        except Exception:
            return False
