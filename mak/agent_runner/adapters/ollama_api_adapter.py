"""Ollama's native API — the local backend that can size its own context.

Every other local runtime is reached through ``local_api`` (the OpenAI-compatible
transport), and that is deliberate: a native adapter per runtime would be a
maintenance surface for a marginal gain. Ollama is the one exception, and this
module exists for one reason.

**Ollama's runtime context defaults to a few thousand tokens regardless of what
the model supports, and it silently truncates an over-long prompt rather than
erroring.** A MAK bundle carries write sources, sibling context and caller
context — tens of KB. So the naive local setup hands a model a third of its task
and gets back a confident, wrong, well-formed answer, with nothing in any log to
say why. The OpenAI-compatible path cannot fix this, because ``num_ctx`` is not
an OpenAI parameter.

This adapter therefore

1. reads the model's **real** context length from ``/api/show`` (cached per
   instance — the number does not change while a process runs),
2. sends an explicit ``options.num_ctx`` sized to the bundle in hand, and
3. **refuses, loudly and non-retryably**, when the bundle cannot fit even that,
   naming the numbers and the two settings that shrink a bundle.

A wrong answer MAK cannot detect becomes a clear failure it can.

Its second reason to exist is cheaper but real: ``format`` takes a JSON schema
that Ollama compiles to a decoding grammar, which on a small model is usually
*more* reliable than cloud-style tool calling — so ``json_schema`` is this
adapter's default structured-output mode rather than an opt-in.
"""

from __future__ import annotations

import json
import math
from typing import Any

from mak.agent_runner.adapters.base_adapter import AgentAdapter
from mak.agent_runner.adapters.budget import resolve_agent_max_tokens
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
from mak.core.exceptions import (
    AgentContextExceededError,
    AgentProtocolError,
)
from mak.core.types import TaskBundle, TaskResult
from mak.local.ollama_client import (
    DEFAULT_BASE_URL,
    OllamaChatResponse,
    OllamaClient,
    OllamaError,
)

_DEFAULT_MODEL = "qwen2.5-coder:14b"

# Constrained decoding is native and free here, so it is the default — unlike the
# OpenAI-compatible transport, where support varies by server.
_DEFAULT_STRUCTURED_OUTPUT = "json_schema"

# A documented heuristic, not a tokenizer. It does not need to be exact, only
# conservative: over-estimating costs memory, under-estimating costs the silent
# truncation this whole module exists to prevent, so the asymmetry is deliberate.
_CHARS_PER_TOKEN = 4

# Margin on top of the estimate, for the same asymmetry.
_CONTEXT_MARGIN = 1.25

# Ollama's own default is smaller than this; never ask for less, because a
# request under it is a request for the failure mode being avoided.
_MIN_NUM_CTX = 4096

# num_ctx is rounded up to a multiple of this so a slightly different prompt does
# not force the server to re-allocate a KV cache of a slightly different size.
_NUM_CTX_GRANULARITY = 512

# The two knobs that shrink a bundle, named in the refusal so the message is
# actionable rather than merely accurate.
_CONTEXT_SETTINGS = (
    "session.dependency_context_bytes / session.cross_file_context_bytes"
)

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


def estimate_tokens(text: str) -> int:
    """Estimate a prompt's token count from its length.

    ``len(text) / 4`` — the standard rough figure for English-ish text and code.
    It under-counts for dense or non-Latin source, which is why every caller
    applies :data:`_CONTEXT_MARGIN` on top.
    """
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


def _round_up(value: int, granularity: int = _NUM_CTX_GRANULARITY) -> int:
    """Round ``value`` up to the next multiple of ``granularity``."""
    return math.ceil(value / granularity) * granularity


class OllamaApiAdapter(AgentAdapter):
    """Agent adapter over Ollama's native ``/api/chat``, with context sizing."""

    agent_type = "ollama_api"

    def __init__(
        self,
        *,
        client: OllamaClient | None = None,
        model: str = _DEFAULT_MODEL,
        api_key: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        agent_id: str = "ollama-0",
        base_url: str | None = None,
        agent_type: str = "ollama_api",
        structured_output: str | None = None,
        repair_attempts: int | None = None,
        num_ctx: int | None = None,
        keep_alive: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.model = model
        self.base_url = base_url or DEFAULT_BASE_URL
        # Explicit rather than Ollama's unlimited default: a small model that
        # starts looping is otherwise bounded only by the timeout.
        self.max_tokens = (
            max_tokens if max_tokens is not None else resolve_agent_max_tokens(model)
        )
        self.timeout = timeout
        self.structured_output = structured_output or _DEFAULT_STRUCTURED_OUTPUT
        self.repair_attempts = 1 if repair_attempts is None else repair_attempts
        # Set = the user's own ceiling, honoured verbatim and enforced strictly.
        # Unset = auto-size per bundle. See ``_effective_num_ctx``.
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.temperature = temperature
        # Accepted and ignored: the composition root passes it to every API
        # adapter, and a local runtime has no key by construction.
        self._api_key = api_key
        self._client = client
        self._model_context: int | None = None
        self._health_detail: str | None = None

    def _get_client(self) -> OllamaClient:
        """Return the HTTP client, building one lazily on first real use."""
        if self._client is None:
            self._client = OllamaClient(
                self.base_url,
                timeout=60.0 if self.timeout is None else self.timeout,
            )
        return self._client

    def format_task(self, task_bundle: TaskBundle) -> str:
        """Serialize the task bundle to the JSON sent as the user message."""
        return encode_task_bundle(task_bundle)

    # -- context sizing (D11) ---------------------------------------------

    def _model_context_length(self) -> int | None:
        """Return the model's real context window, asking ``/api/show`` once.

        Cached on the instance: it cannot change while the process runs, and the
        adapter is rebuilt per dispatch anyway. A server that cannot answer is
        not a failure here — the caller falls back to the configured or
        estimated window rather than refusing to run at all.
        """
        if self._model_context is None:
            try:
                self._model_context = self._get_client().show(self.model).context_length
            except OllamaError:
                return None
        return self._model_context

    def _effective_num_ctx(self, prompt: str) -> int:
        """Return the ``num_ctx`` to request for ``prompt``, or refuse.

        A configured ``num_ctx`` is used verbatim and enforced as a hard ceiling
        — the user asked for exactly that window, and silently exceeding it is
        the behaviour this adapter exists to prevent. Unset, the window is sized
        to the bundle and capped at what the model actually supports.
        """
        needed = math.ceil(estimate_tokens(prompt) * _CONTEXT_MARGIN) + self.max_tokens
        limit = self._model_context_length()
        if self.num_ctx is not None:
            if needed > self.num_ctx:
                raise self._context_error(prompt, needed, self.num_ctx, "num_ctx")
            return self.num_ctx
        if limit is None:
            # The server did not report a window. Size to the bundle and let the
            # load fail loudly (as an OllamaError naming num_ctx) if the host
            # cannot hold it — better than quietly asking for a small window.
            return max(_MIN_NUM_CTX, _round_up(needed))
        if needed > limit:
            raise self._context_error(prompt, needed, limit, "model")
        return max(_MIN_NUM_CTX, min(limit, _round_up(needed)))

    def _context_error(
        self, prompt: str, needed: int, limit: int, source: str
    ) -> AgentContextExceededError:
        """Build the refusal, naming the numbers and the settings that fix it."""
        where = (
            f"the configured num_ctx of {limit}"
            if source == "num_ctx"
            else f"{self.model}'s context window of {limit} tokens"
        )
        remedy = (
            f"raise num_ctx, or shrink the bundle with {_CONTEXT_SETTINGS}"
            if source == "num_ctx"
            else (
                f"shrink the bundle with {_CONTEXT_SETTINGS}, or use a model with "
                "a larger context window"
            )
        )
        return AgentContextExceededError(
            f"this task's bundle needs about {needed} tokens "
            f"({len(prompt)} characters of prompt plus a {self.max_tokens}-token "
            f"reply), which does not fit {where}. Ollama would truncate it "
            f"silently and answer from a partial task, so MAK refused. To fix: "
            f"{remedy}."
        )

    # -- dispatch ----------------------------------------------------------

    def _format_option(self) -> dict[str, Any] | str | None:
        """Return Ollama's ``format`` for the configured structured-output mode.

        A grammar constrains syntax, not intent — the system prompt still
        describes the shape in words in every mode.
        """
        if self.structured_output == "json_schema":
            return result_schema("ollama")
        if self.structured_output == "json_object":
            return "json"
        return None

    def _options(self, num_ctx: int) -> dict[str, Any]:
        """Return the ``options`` block for one request."""
        options: dict[str, Any] = {
            "num_ctx": num_ctx,
            "num_predict": self.max_tokens,
        }
        if self.temperature is not None:
            # Unset leaves the server's own default, which is tuned for chat.
            options["temperature"] = self.temperature
        return options

    def send(self, prompt: str) -> str:
        """Size the context, call ``/api/chat``, and repair a malformed reply."""
        client = self._get_client()
        num_ctx = self._effective_num_ctx(prompt)
        messages: Messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        def call(msgs: Messages) -> OllamaChatResponse:
            return client.chat(
                model=self.model,
                messages=msgs,
                format=self._format_option(),
                options=self._options(num_ctx),
                keep_alive=self.keep_alive,
                timeout=self.timeout,
            )

        return repair_loop(
            messages,
            call=call,
            read_meta=self._read_meta,
            extract=self._extract_content,
            follow_up=_follow_up,
            repair_attempts=self.repair_attempts,
        )

    def _read_meta(self, response: OllamaChatResponse) -> ResponseMeta:
        """Return ``(usage, done_reason, raw_text)``, rejecting a cut reply.

        ``done_reason`` goes through the same ``check_stop_reason`` every other
        backend uses — ``"length"`` is already a truncation signal there — so a
        cut local reply is rejected by one rule rather than a local variant.
        """
        usage = extract_usage(response)
        check_stop_reason(
            response.done_reason,
            provider="ollama",
            budget=self.max_tokens,
            usage=usage,
        )
        return usage, response.done_reason, response.content

    def _extract_content(self, response: OllamaChatResponse) -> str:
        """Normalize the reply's JSON body, or say why it is not usable."""
        usage = extract_usage(response)
        detail: dict[str, Any] = {
            "stop_reason": response.done_reason,
            "usage": usage,
        }
        if not response.content.strip():
            raise AgentProtocolError("ollama response had no content", **detail)
        try:
            payload = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise AgentProtocolError(
                f"ollama response was not valid JSON: {exc}", **detail
            ) from exc
        if not isinstance(payload, dict):
            raise AgentProtocolError(
                "ollama response JSON was not an object (got "
                f"{type(payload).__name__})",
                **detail,
            )
        payload["protocol_version"] = PROTOCOL_VERSION
        return json.dumps(payload)

    def parse_result(self, raw_output: str) -> TaskResult:
        """Decode the JSON payload into a ``TaskResult``."""
        return decode_task_result(raw_output)

    # -- preflight ---------------------------------------------------------

    def health_check(self) -> bool:
        """Return whether Ollama is running **and** has the configured model.

        Both halves matter and they fail for different reasons, so
        :meth:`health_detail` distinguishes them. Without this preflight, "the
        server isn't running" and "you never pulled that model" both surface as
        three failed dispatch attempts per task.
        """
        client = self._get_client()
        try:
            client.version()
        except OllamaError as exc:
            self._health_detail = f"Ollama is not running at {self.base_url} ({exc})"
            return False
        try:
            installed = {model.name for model in client.list_models()}
        except OllamaError as exc:
            self._health_detail = (
                f"Ollama at {self.base_url} could not list models: {exc}"
            )
            return False
        if self.model not in installed:
            self._health_detail = (
                f"model '{self.model}' is not pulled — run "
                f"`ollama pull {self.model}` or `/local pull {self.model}`"
            )
            return False
        self._health_detail = None
        return True

    def health_detail(self) -> str | None:
        """Return why the last ``health_check`` failed, if it did."""
        return self._health_detail


def _follow_up(messages: Messages, raw_text: str, instruction: str) -> Messages:
    """Append the model's own previous reply plus the repair instruction."""
    return [
        *messages,
        {"role": "assistant", "content": raw_text},
        {"role": "user", "content": instruction},
    ]
