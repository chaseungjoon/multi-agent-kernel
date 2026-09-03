"""OpenAI Chat Completions adapter — cloud OpenAI *and* every local server.

Like the Anthropic adapter, this talks to the API directly and forces structured
output. The model is instructed to emit exactly the ``TaskResult`` field set,
which is then decoded through MAK's wire protocol — no stdout scraping.

**Two agent types, one class.** Nearly every local runtime (Ollama's compat
layer, vLLM, llama.cpp's server, LM Studio, LocalAI) speaks this same wire
format, so pointing the SDK at a ``base_url`` is all the transport a local model
needs. It is registered twice, though: as ``openai_api`` (``base_url`` optional
— a gateway or proxy) and as ``local_api`` (``base_url`` required). The registry
is keyed by agent *type*, so with a single type a run could have cloud OpenAI
**or** a local model and never both, and nothing downstream — the health
preflight, the planner's agent-type list, warnings, logs, the TUI — could tell a
local run from a cloud one. The instance therefore reports the type it was built
as.

**The key is never leaked.** With a ``base_url`` set, MAK sends the configured
``api_key_env``'s value if the user named one and the literal placeholder
``"local"`` otherwise — and always sends *something*, so the SDK can never fall
back to reading ``OPENAI_API_KEY`` from the environment and POSTing a real key to
whatever host the config names. This is the one security property of the local
transport and it has its own tests at unit and acceptance level.

**Output budget.** No cap is sent unless one is configured, so the model's own
maximum applies — the better default. The *field name* differs by transport
(``max_completion_tokens`` for cloud OpenAI, ``max_tokens`` for a ``base_url``
endpoint), because the compat layers in Ollama and llama.cpp implement only the
older name: sending the newer one there either 400s or, worse, is ignored and
the cap silently does not exist. ``finish_reason`` is still read either way: a
length-truncated JSON-mode reply usually fails as invalid JSON, but "usually" is
not a contract, and a cut landing on a closing brace would decode as a
successful result with no work in it.

The SDK is imported lazily and the client is injectable, so neither the adapter
nor its tests require the ``openai`` package unless a real call is made.
"""

from __future__ import annotations

import json
from typing import Any

from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.agent_runner.adapters.repair import Messages, ResponseMeta, repair_loop
from mak.agent_runner.adapters.result_schema import result_schema
from mak.agent_runner.protocol import (
    NO_CHANGE_CONTRACT,
    NODE_ID_CONTRACT,
    PROTOCOL_VERSION,
    RETRY_NOTE_CONTRACT,
    decode_task_result,
    encode_task_bundle,
)
from mak.agent_runner.stop_signals import check_stop_reason, extract_usage
from mak.core.exceptions import AgentError, AgentProtocolError
from mak.core.types import TaskBundle, TaskResult

_DEFAULT_MODEL = "gpt-5.6-sol"

# Sent as the API key whenever a ``base_url`` is configured and no key env var
# was named. The SDK requires *some* key; this one is deliberately not a secret,
# and sending it is what stops the SDK reading a real one from the environment.
_LOCAL_PLACEHOLDER_KEY = "local"

# How the structured-output modes step down when a server rejects the one asked
# for. Capability varies across Ollama, vLLM, llama.cpp, and LM Studio, and the
# failure mode should be a slower call, not a dead run.
_STRUCTURED_OUTPUT_DOWNGRADE: dict[str, str] = {
    "json_schema": "json_object",
    "json_object": "none",
}

_DEFAULT_STRUCTURED_OUTPUT = "json_object"

# Substrings that mark a rejection of the *response format* specifically, as
# opposed to any other 4xx. Matched case-insensitively against the error text,
# because no local server reports this in a structured way.
_FORMAT_REJECTION_MARKERS = (
    "response_format",
    "json_schema",
    "unsupported",
    "not supported",
)

_JSON_SCHEMA_NAME = "task_result"

_SYSTEM_PROMPT = (
    "You are a MAK coding agent. You receive a single task as a JSON 'task "
    "bundle' (task_id, description, the node ids you may modify, and read-only "
    "context whose 'write_source:<id>' / 'read_source:<id>' entries hold the "
    "current source). Carry out the task, then respond with a JSON object "
    "containing exactly these keys: 'task_id' (string, echoing the bundle's "
    "task_id), 'success' (boolean), 'modified_fragments' (array of objects, each "
    "with 'node_id' and the FULL rewritten 'new_source' of that node — complete "
    "source, never a diff, only for nodes you may modify), "
    "'no_changes_required' (boolean), and 'error' (string reason when success is "
    f"false, otherwise null). {NODE_ID_CONTRACT} {NO_CHANGE_CONTRACT} "
    f"{RETRY_NOTE_CONTRACT} Respond with only that JSON object."
)


def _is_format_rejection(exc: Exception) -> bool:
    """Whether an SDK error reads as "I do not support that response format"."""
    text = str(exc).lower()
    return any(marker in text for marker in _FORMAT_REJECTION_MARKERS)


class OpenAiApiAdapter(AgentAdapter):
    """OpenAI Chat Completions adapter — cloud, gateway, or local endpoint."""

    agent_type = "openai_api"

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        api_key: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        agent_id: str = "openai-0",
        base_url: str | None = None,
        agent_type: str = "openai_api",
        structured_output: str | None = None,
        repair_attempts: int | None = None,
    ) -> None:
        self.agent_id = agent_id
        # Shadows the class attribute so a ``local_api`` instance reports its own
        # type everywhere the kernel asks (health preflight, logs, the TUI).
        self.agent_type = agent_type
        self.model = model
        # None = send no cap and inherit the model's own maximum. Only a
        # configured value is forwarded, so a user on a small or metered model can
        # bound the spend without every other user being silently clipped.
        self.max_tokens = max_tokens
        # Seconds; see the Anthropic adapter for why an unbounded call is worse
        # than a slow one.
        self.timeout = timeout
        self.base_url = base_url
        self.structured_output = structured_output or _DEFAULT_STRUCTURED_OUTPUT
        # One follow-up turn by default: it fires only on a reply that is
        # *already* a failed attempt, and one short turn is far cheaper than the
        # whole-bundle re-dispatch it replaces. ``0`` switches it off.
        self.repair_attempts = 1 if repair_attempts is None else repair_attempts
        self._api_key = api_key
        self._client = client
        self._health_detail: str | None = None

    def _get_client(self) -> Any:
        """Return the SDK client, constructing one lazily on first real use."""
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - exercised via health_check
                raise AgentError(
                    "openai SDK not installed; run "
                    "'pip install \"multi-agent-kernel[openai]\"'"
                ) from exc
            options: dict[str, Any] = {}
            if self.base_url is not None:
                options["base_url"] = self.base_url
                # Always explicit — see the module docstring. Omitting it here
                # is what would let the SDK read OPENAI_API_KEY itself and send
                # the user's real cloud key to a third-party host.
                options["api_key"] = self._api_key or _LOCAL_PLACEHOLDER_KEY
            elif self._api_key is not None:
                options["api_key"] = self._api_key
            if self.timeout is not None:
                options["timeout"] = self.timeout
            self._client = openai.OpenAI(**options)
        return self._client

    def format_task(self, task_bundle: TaskBundle) -> str:
        """Serialize the task bundle to the JSON sent as the user message."""
        return encode_task_bundle(task_bundle)

    def _response_format(self, mode: str) -> dict[str, Any] | None:
        """Return the ``response_format`` kwarg for one structured-output mode."""
        if mode == "json_object":
            return {"type": "json_object"}
        if mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": _JSON_SCHEMA_NAME,
                    "schema": result_schema("openai"),
                    "strict": True,
                },
            }
        # "none": the system prompt already demands a bare JSON object, which is
        # all a server with no structured-output support can be asked for.
        return None

    def _create(self, client: Any, messages: Messages, mode: str) -> Any:
        """Make one Chat Completions call in ``mode``."""
        extra: dict[str, Any] = {}
        if self.max_tokens is not None:
            # D4: the compat layers implement only the older field name, so on a
            # base_url endpoint the newer one is at best ignored.
            field = (
                "max_tokens" if self.base_url is not None else "max_completion_tokens"
            )
            extra[field] = self.max_tokens
        response_format = self._response_format(mode)
        if response_format is not None:
            extra["response_format"] = response_format
        return client.chat.completions.create(
            model=self.model,
            messages=messages,
            **extra,
        )

    def send(self, prompt: str) -> str:
        """Call Chat Completions and return the decodable result JSON.

        Two bounded recoveries wrap the single call, both per-dispatch and
        neither remembered afterwards (the registry rebuilds adapters per
        dispatch, so anything kept across calls would be global mutable state):

        - a server that rejects the requested ``response_format`` gets **one**
          retry a rung down (``json_schema`` → ``json_object`` → ``none``);
        - a reply that arrives but cannot be decoded gets ``repair_attempts``
          short follow-up turns rather than a whole-bundle re-dispatch.
        """
        client = self._get_client()
        messages: Messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        mode = self.structured_output
        downgraded = False

        def call(msgs: Messages) -> Any:
            nonlocal mode, downgraded
            try:
                return self._create(client, msgs, mode)
            except Exception as exc:
                next_mode = _STRUCTURED_OUTPUT_DOWNGRADE.get(mode)
                if downgraded or next_mode is None or not _is_format_rejection(exc):
                    raise
                downgraded = True
                mode = next_mode
                return self._create(client, msgs, mode)

        return repair_loop(
            messages,
            call=call,
            read_meta=self._read_meta,
            extract=self._extract_content,
            follow_up=_follow_up,
            repair_attempts=self.repair_attempts,
        )

    def _read_meta(self, response: Any) -> ResponseMeta:
        """Return ``(usage, finish_reason, raw_text)``, rejecting a cut reply.

        Runs before any payload is read, so a truncation or a refusal can never
        be mistaken for a result — and can never be "repaired", since both
        repeat verbatim on the same request.
        """
        usage = extract_usage(getattr(response, "usage", None))
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise AgentProtocolError(
                "openai response contained no choices", usage=usage
            )
        finish_reason = getattr(choices[0], "finish_reason", None)
        check_stop_reason(
            finish_reason,
            provider="openai",
            budget=self.max_tokens,
            usage=usage,
        )
        raw_text = getattr(choices[0].message, "content", None) or ""
        return usage, finish_reason, raw_text

    def _extract_content(self, response: Any) -> str:
        """Pull the JSON content out of the first choice and normalize it.

        Every rejection here is an ``AgentProtocolError``, not a bare
        ``AgentError``: the HTTP call succeeded and the model replied, so the
        failure is a malformed *body*, and only that classification reaches the
        session's schema-restating retry note. Reported as ``api`` it drew the
        generic "that produced nothing usable" note instead — which never told
        the model what shape was wanted.
        """
        usage = extract_usage(getattr(response, "usage", None))
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise AgentProtocolError(
                "openai response contained no choices", usage=usage
            )
        choice = choices[0]
        stop_reason = getattr(choice, "finish_reason", None)
        detail: dict[str, Any] = {
            "stop_reason": None if stop_reason is None else str(stop_reason),
            "usage": usage,
        }
        content = choice.message.content
        if content is None:
            raise AgentProtocolError(
                "openai response message had no content", **detail
            )
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AgentProtocolError(
                f"openai response was not valid JSON: {exc}", **detail
            ) from exc
        if not isinstance(payload, dict):
            raise AgentProtocolError(
                "openai response JSON was not an object (got "
                f"{type(payload).__name__})",
                **detail,
            )
        payload["protocol_version"] = PROTOCOL_VERSION
        return json.dumps(payload)

    def parse_result(self, raw_output: str) -> TaskResult:
        """Decode the JSON payload into a ``TaskResult``."""
        return decode_task_result(raw_output)

    def health_check(self) -> bool:
        """Return whether the backend is usable, without dispatching a task.

        For a cloud endpoint, constructing the client *is* the whole check —
        building a registry must stay free of network calls. For a ``base_url``
        endpoint it is not enough: "the server isn't running" is the single most
        likely local failure, and without a probe it surfaces as three failed
        dispatch attempts per task instead of one line at startup.
        """
        try:
            client = self._get_client()
        except Exception as exc:
            self._health_detail = str(exc)
            return False
        if self.base_url is None:
            self._health_detail = None
            return True
        try:
            _probe_models(client)
        except Exception as exc:
            self._health_detail = (
                f"no OpenAI-compatible server answered at {self.base_url} ({exc})"
            )
            return False
        self._health_detail = None
        return True

    def health_detail(self) -> str | None:
        """Return why the last ``health_check`` failed, if it did.

        Read by ``mak.bootstrap.healthy_agent_types`` so the startup warning can
        name the endpoint instead of guessing at "missing key/SDK, or CLI not on
        PATH" — which is never the reason a local server is unreachable.
        """
        return self._health_detail


def _probe_models(client: Any) -> None:
    """List models once, with a short timeout, to prove the endpoint answers."""
    try:
        probe = client.with_options(timeout=5.0)
    except AttributeError:
        # An injected fake need not implement ``with_options``; the listing call
        # below is the part that matters.
        probe = client
    probe.models.list()


def _follow_up(messages: Messages, raw_text: str, instruction: str) -> Messages:
    """Append the model's own previous reply plus the repair instruction."""
    return [
        *messages,
        {"role": "assistant", "content": raw_text},
        {"role": "user", "content": instruction},
    ]
