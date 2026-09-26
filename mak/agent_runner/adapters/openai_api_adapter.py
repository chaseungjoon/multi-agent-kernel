"""OpenAI Chat Completions adapter — the ``openai_chat`` transport.

Like the Anthropic adapter, this talks to the API directly and forces structured
output. The model is instructed to emit exactly the ``TaskResult`` field set,
which is then decoded through MAK's wire protocol — no stdout scraping.

**One class, any number of endpoints.** Cloud OpenAI, NVIDIA Build,
OpenRouter, DeepSeek, Z.ai, vLLM, llama.cpp, LM Studio and Ollama's compat layer
all speak this wire format, so they differ only in a URL, a credential, and
which capability rungs they support — never in code. The registry keys on agent
*id*, so a run may hold as many of them at once as the user configures; the
adapter simply reports the id it was built under.

**Nothing is inferred from the URL.** The old rule "a ``base_url`` means this is
local" was wrong the moment a hosted compatible service appeared: NVIDIA and
OpenRouter have base URLs and bill a real account. Location, token-parameter
name, structured-output policy and health policy now all arrive **resolved**
from ``mak.endpoints``, decided once at composition time.

**The key is never leaked.** With a ``base_url`` set, MAK sends the resolved
key if the endpoint named a credential variable and the literal placeholder
``"local"`` otherwise — and always sends *something*, so the SDK can never fall
back to reading ``OPENAI_API_KEY`` from the environment and POSTing a real key to
whatever host the config names. This is the one security property of the
transport and it has its own tests at unit and acceptance level.

**Output budget.** No cap is sent unless one is configured, so the model's own
maximum applies — the better default. The *field name* comes from the endpoint's
resolved token policy: cloud OpenAI wants ``max_completion_tokens``, while most
compatible layers implement only the older ``max_tokens``, where the newer name
either 400s or, worse, is ignored and the cap silently does not exist.
``finish_reason`` is still read either way: a length-truncated JSON-mode reply
usually fails as invalid JSON, but "usually" is not a contract, and a cut landing
on a closing brace would decode as a successful result with no work in it.

The SDK is imported lazily and the client is injectable, so neither the adapter
nor its tests require the ``openai`` package unless a real call is made.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
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
from mak.endpoints.capabilities import (
    STRUCTURED_OUTPUT_LADDER,
    CapabilityCache,
    Discovery,
    rung_is_reported,
    rungs_from,
    start_rung_for,
)
from mak.endpoints.error_classification import classify_rejection
from mak.endpoints.health import NOT_PROBED, classify_failure

_LOG = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-5.6-sol"

# Sent as the API key whenever a ``base_url`` is configured and no key env var
# was named. The SDK requires *some* key; this one is deliberately not a secret,
# and sending it is what stops the SDK reading a real one from the environment.
_LOCAL_PLACEHOLDER_KEY = "local"


_DEFAULT_STRUCTURED_OUTPUT = "json_object"

# "Start at the best rung and find out." Distinct from a named mode, which is a
# statement the user made and which the ladder never climbs above.
_AUTO_STRUCTURED_OUTPUT = "auto"

# Output-cap field names, mirroring ``mak.endpoints.types.TokenParameter``.
# Duplicated as plain strings rather than imported so this adapter stays usable
# from a bare construction in a test without pulling the endpoint package in;
# a contract test pins the two sets together.
TOKEN_PARAM_AUTO = "auto"
TOKEN_PARAM_NONE = "none"
TOKEN_PARAM_MAX_TOKENS = "max_tokens"
TOKEN_PARAM_MAX_COMPLETION_TOKENS = "max_completion_tokens"

# Health policies, mirroring ``mak.endpoints.types.HealthPolicy`` as plain
# strings for the same reason the token names are; the contract test pins them.
HEALTH_MODELS = "models"
HEALTH_CHAT = "chat"
HEALTH_NONE = "none"

# The adapter's own default, deliberately **not** one of the endpoint policies:
# "probe if there is an address of our own to probe, otherwise treat a successful
# client construction as the check". It preserves the rule that building a
# registry makes no network call for the SDK's default host, and it is what a
# bare construction in a test gets. A real run always passes the endpoint's
# resolved policy explicitly, so this value never decides anything in production.
HEALTH_AUTO = "auto"

# Provider-routing policies, mirroring ``mak.endpoints.types.ProviderRouting``
# as plain strings for the same reason the token names above are; the contract
# test pins them together.
ROUTING_NONE = "none"
ROUTING_OPENROUTER = "openrouter"

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
    """Whether an SDK error is verifiably "I do not support that reply format".

    Kept as a module-level function because the test suite and third-party
    code both call it. The judgment itself now lives in
    ``mak.endpoints.error_classification``, which parses the provider's
    structured error body instead of grepping ``str(exc)`` for a tuple of
    literal spellings.

    That tuple was the 0.8.1 bug: OpenRouter refused the same model twice with
    ``structured outputs`` and ``structured-outputs``, and only the first
    spelling was listed. Extending it was never going to work — the sentence is
    written by whichever upstream provider OpenRouter picked.
    """
    return classify_rejection(exc).is_format_rejection


class OpenAiCompatibleAdapter(AgentAdapter):
    """OpenAI Chat Completions adapter — any endpoint speaking that protocol."""

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
        token_parameter: str = TOKEN_PARAM_AUTO,
        provider_routing: str = ROUTING_NONE,
        headers: tuple[tuple[str, str], ...] = (),
        endpoint_id: str = "",
        endpoint_name: str = "",
        capabilities: CapabilityCache | None = None,
        health_check_policy: str = HEALTH_AUTO,
        chat_probe_ok: bool = False,
        api_key_env: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        # The *transport*, not the routing key: several agents may share it.
        # ``agent_id`` above is what the registry, scheduler and logs key on.
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
        # Which output-cap field name this endpoint accepts. Resolved upstream
        # from the endpoint's profile; ``auto`` keeps the historical rule.
        self.token_parameter = token_parameter
        # Whether this endpoint's body may carry a provider-routing extension.
        # Resolved upstream from the profile, never from the URL: a user can
        # proxy OpenRouter or point a custom endpoint at the same host, and a
        # hostname check gives the wrong answer in both cases.
        self.provider_routing = provider_routing
        # Extra request headers, already resolved to literal values with any
        # unset secret dropped. Names only ever reach status output and logs.
        self.headers = headers
        # For messages and the capability cache key. Two endpoints can offer the
        # same model id and are still different choices.
        self.endpoint_id = endpoint_id or agent_type
        self.endpoint_name = endpoint_name or self.endpoint_id
        # One follow-up turn by default: it fires only on a reply that is
        # *already* a failed attempt, and one short turn is far cheaper than the
        # whole-bundle re-dispatch it replaces. ``0`` switches it off.
        self.repair_attempts = 1 if repair_attempts is None else repair_attempts
        self._api_key = api_key
        self._client = client
        self._health_detail: str | None = None
        # Session-lifetime, injected by the composition root. ``None`` means
        # "no memory" — each dispatch rediscovers, which is correct for a bare
        # construction in a test and never for a real run.
        self._capabilities = capabilities
        self.health_check_policy = health_check_policy
        # Whether the user has accepted that a chat probe may be billed. Without
        # it a ``chat`` policy degrades to not-probed rather than spending.
        self.chat_probe_ok = chat_probe_ok
        # Named only so a credential failure can say *which* variable to check.
        self.api_key_env = api_key_env
        self._not_probed = False

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
            if self.headers:
                # Validated upstream: MAK's own headers (Authorization,
                # Content-Type, Host, User-Agent) cannot be overridden here, so
                # this can add metadata but never re-route the credential.
                options["default_headers"] = dict(self.headers)
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

    def _token_field(self) -> str | None:
        """Return the output-cap field name to send, or None to send no cap.

        ``auto`` reproduces the historical rule — ``max_completion_tokens`` for
        the official OpenAI endpoint, ``max_tokens`` for anything with a
        ``base_url`` — because the compat layers implement only the older name,
        where the newer one is at best ignored and the cap silently does not
        exist. An endpoint that knows better states it and is believed.
        """
        if self.token_parameter == TOKEN_PARAM_NONE:
            return None
        if self.token_parameter == TOKEN_PARAM_AUTO:
            return (
                TOKEN_PARAM_MAX_TOKENS
                if self.base_url is not None
                else TOKEN_PARAM_MAX_COMPLETION_TOKENS
            )
        return self.token_parameter

    def _routing_guard(self, mode: str, reported: frozenset[str] | None) -> bool:
        """Whether to ask OpenRouter to route only to a capable provider.

        Three conditions, all required:

        * the endpoint's profile says it understands the extension, so the
          ``provider`` object never reaches OpenAI, NVIDIA, DeepSeek, Z.ai,
          vLLM, llama.cpp or a custom endpoint;
        * the rung actually sends a ``response_format``, since there is nothing
          to require on the prompt-only rung;
        * the catalog **positively confirms** the parameter backing this rung.

        The third condition is the one that is easy to get wrong. The guard
        rejects by *routing*, so when it filters every provider away OpenRouter
        answers 404 — outside the 400/422 window a format rejection lives in.
        Sent on a model MAK knows nothing about, it would therefore convert a
        recoverable provider rejection into a hard failure the ladder cannot
        descend from, which is strictly worse than not sending it. Sent only
        where the catalog says it will hold, it keeps routing off an incapable
        sibling provider and cannot manufacture that 404.
        """
        if self.provider_routing != ROUTING_OPENROUTER:
            return False
        if self._response_format(mode) is None:
            return False
        return rung_is_reported(mode, reported)

    def _create(
        self, client: Any, messages: Messages, mode: str, *, guard: bool = False
    ) -> Any:
        """Make one Chat Completions call in ``mode``.

        ``guard`` adds OpenRouter's ``provider.require_parameters`` through the
        SDK's ``extra_body``, which is how a documented vendor extension is
        sent without adopting a second SDK or loosening the typing of the
        ordinary OpenAI parameters beside it.
        """
        extra: dict[str, Any] = {}
        field = self._token_field()
        if self.max_tokens is not None and field is not None:
            extra[field] = self.max_tokens
        response_format = self._response_format(mode)
        if response_format is not None:
            extra["response_format"] = response_format
        if guard:
            extra["extra_body"] = {"provider": {"require_parameters": True}}
        return client.chat.completions.create(
            model=self.model,
            messages=messages,
            **extra,
        )

    def _configured_rungs(self) -> tuple[str, ...]:
        """Return the descent path the user's own configuration allows.

        ``auto`` walks the full ladder from ``json_schema`` down. An explicitly
        named mode walks from itself down, and never above — asking for a
        *stronger* contract than the user configured would ignore a deliberate
        choice, and is the rule that keeps catalog seeding and another agent's
        discovery from silently re-enabling a mode the user switched off.
        """
        if self.structured_output == _AUTO_STRUCTURED_OUTPUT:
            return rungs_from(STRUCTURED_OUTPUT_LADDER[0])
        return rungs_from(self.structured_output)

    def _rungs_for(self, discovery: Discovery) -> tuple[str, ...]:
        """Return the modes to try for this dispatch, best first.

        Three sources of evidence, in decreasing authority:

        1. **A proven mode** — this session made a successful call in it, or
           waited on the agent that did. It pins exactly: the session already
           paid to discover it and paying again per task is the cost this cache
           exists to remove.
        2. **The endpoint's reported parameters** — a claim from its ``/models``
           listing. It lowers the *starting* rung and leaves descent below
           available, because a claim is not a proof. This is what turns the
           incident model from three failing calls per task into one working
           one, before any call is made.
        3. **Configuration alone** — the historical bounded probe ladder, which
           is correct for the many endpoints that publish no capability data.

        Both (1) and (2) are clamped to what configuration allows, so a mode
        learned for another agent on the same pair can never raise this agent
        above the ceiling its own ``structured_output`` set.
        """
        allowed = self._configured_rungs()
        if discovery.mode is not None and discovery.mode in allowed:
            return (discovery.mode,)
        seeded = start_rung_for(discovery.reported)
        if seeded is not None and seeded in allowed:
            return rungs_from(seeded)
        return allowed

    def send(self, prompt: str) -> str:
        """Call Chat Completions and return the decodable result JSON.

        Three bounded recoveries wrap the call:

        - a server that rejects the requested ``response_format`` steps **all
          the way** down the ladder (``json_schema`` → ``json_object`` →
          ``none``) rather than once. A single downgrade made prompt-only JSON
          unreachable from the top rung, so an endpoint supporting neither
          schema nor object mode failed every task;
        - a request whose OpenRouter routing guard left no eligible provider
          retries the *same* rung once without the guard, rather than descending.
          The guard is a routing preference, not a capability fact, and a model
          may well serve a rung its aggregated metadata failed to promise;
        - a reply that arrives but cannot be decoded gets ``repair_attempts``
          short follow-up turns rather than a whole-bundle re-dispatch.

        The whole ladder runs under a single-flight lease, so four agents
        starting together pay for one discovery between them instead of four.
        The winning rung is recorded in the session's capability cache, so the
        next task starts where this one finished instead of rediscovering it.
        """
        client = self._get_client()
        messages: Messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        with self._discovering() as discovery:
            rungs = self._rungs_for(discovery)

            def call(msgs: Messages) -> Any:
                return self._walk(client, msgs, rungs, discovery.reported)

            return repair_loop(
                messages,
                call=call,
                read_meta=self._read_meta,
                extract=self._extract_content,
                follow_up=_follow_up,
                repair_attempts=self.repair_attempts,
            )

    @contextmanager
    def _discovering(self) -> Iterator[Discovery]:
        """Hold a single-flight discovery lease, or a null one with no cache.

        A bare construction in a test has no capability cache, and must still
        work: it simply discovers every time, which is what "no memory" means.
        """
        if self._capabilities is None:
            yield Discovery(owned=True)
            return
        with self._capabilities.discovering(self.endpoint_id, self.model) as lease:
            yield lease

    def _walk(
        self,
        client: Any,
        msgs: Messages,
        rungs: tuple[str, ...],
        reported: frozenset[str] | None,
    ) -> Any:
        """Walk the ladder once, returning the first response that comes back.

        Only a *verified* rejection moves, and only while a lower rung exists.
        Anything else — an auth failure, a missing model, a quota breach, an
        invalid schema MAK authored, a 5xx, a transport error — is the
        provider's real answer and is raised unchanged. Answering those by
        asking more quietly is how one clear error used to become a confusing
        second one a rung down.
        """
        descended = False
        for index, mode in enumerate(rungs):
            guard = self._routing_guard(mode, reported)
            for attempt_guard in ((True, False) if guard else (False,)):
                try:
                    response = self._create(
                        client, msgs, mode, guard=attempt_guard
                    )
                except Exception as exc:
                    analysis = classify_rejection(exc)
                    if attempt_guard and analysis.is_routing_rejection:
                        # MAK's own guard excluded every provider. Drop it and
                        # ask the same rung plainly before giving up on it.
                        self._note_routing_retry(mode, analysis.reason)
                        continue
                    if index + 1 >= len(rungs) or not analysis.is_format_rejection:
                        raise
                    descended = True
                    break
                if descended:
                    self._note_rung(mode, "runtime_rejection")
                self._remember(mode)
                return response
        raise AgentError(  # pragma: no cover - the loop always returns or raises
            "structured-output ladder exhausted without an outcome"
        )

    def _remember(self, mode: str) -> None:
        """Record the mode that worked for this endpoint/model pair."""
        if self._capabilities is not None:
            self._capabilities.record_structured_output(
                self.endpoint_id, self.model, mode
            )

    def _note_rung(self, mode: str, evidence: str) -> None:
        """Log the selected rung once per endpoint/model pair, with its source.

        ``evidence`` is ``catalog`` when the endpoint's own model listing ruled
        the higher rungs out before any call, and ``runtime_rejection`` when the
        provider refused them. The distinction is the first thing worth knowing
        when a user asks why their model is not getting schema enforcement:
        one is a published fact they can look up, the other is a discovery that
        may be stale by tomorrow.

        Carries the endpoint id and the exact model id and **nothing else**. No
        response body, no headers, no prompt content, no key: a provider error
        body can echo any of those, and this line goes to a log file the user
        may paste into an issue.
        """
        if self._capabilities is None:
            return
        if not self._capabilities.should_announce(self.endpoint_id, self.model):
            return
        _LOG.info(
            "%s: '%s' will use reply format '%s' for the rest of this session "
            "(evidence: %s)",
            self.endpoint_name,
            self.model,
            mode,
            evidence,
        )

    def _note_downgrade(self, mode: str) -> None:
        """Log a descent once per endpoint/model pair.

        Retained under its original name for existing callers; ``_note_rung``
        supersedes it and records *why* the rung was chosen as well as which
        it was.
        """
        self._note_rung(mode, "runtime_rejection")

    def _note_routing_retry(self, mode: str, reason: str) -> None:
        """Log that the routing guard was dropped for one request.

        Debug rather than info: it is a normal, self-healing consequence of
        OpenRouter's aggregated model metadata disagreeing with the endpoint it
        actually picked, and it costs one extra request, not a capability.

        ``reason`` has already been redacted and bounded by the classifier.
        """
        _LOG.debug(
            "%s: '%s' had no provider accepting the parameters for reply "
            "format '%s'; retrying without the routing guard (%s)",
            self.endpoint_name,
            self.model,
            mode,
            reason,
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

        What "usable" means is the endpoint's configured policy, not a guess:

        * ``models`` — list models once with a short timeout. The default for a
          compatible endpoint, because "the server isn't running" is the most
          likely failure and without a probe it surfaces as three failed
          dispatch attempts per task instead of one line at startup.
        * ``chat`` — a tiny real completion. It costs money, so it runs **only**
          after the user has explicitly accepted that; without the acceptance
          this degrades to not-probed rather than silently billing them.
        * ``none`` — validate construction only, make no network call, and
          report *not probed*. A service with no ``/models`` route is perfectly
          usable through manual model entry, and dropping it from the pool for
          lacking a listing it never claimed would be wrong.

        Constructing the client is always checked first: a missing SDK or an
        unbuildable client is a failure under every policy.
        """
        try:
            client = self._get_client()
        except Exception as exc:
            self._health_detail = classify_failure(
                exc,
                endpoint_id=self.endpoint_id,
                api_key_env=self.api_key_env,
                base_url=self.base_url,
            ).message()
            return False

        policy = self.health_check_policy
        if policy == HEALTH_AUTO:
            # Nothing of our own to probe means the SDK's default host, where a
            # startup listing would be a network call MAK has never made.
            policy = HEALTH_MODELS if self.base_url is not None else HEALTH_NONE
        if policy == HEALTH_NONE or (policy == HEALTH_CHAT and not self.chat_probe_ok):
            # Not probed is not the same as healthy, and health_status says which.
            self._health_detail = None
            self._not_probed = True
            return True
        self._not_probed = False

        try:
            if policy == HEALTH_CHAT:
                _probe_chat(client, self.model)
            else:
                _probe_models(client)
        except Exception as exc:
            self._health_detail = classify_failure(
                exc,
                endpoint_id=self.endpoint_id,
                api_key_env=self.api_key_env,
                base_url=self.base_url,
            ).message()
            return False
        self._health_detail = None
        return True

    def health_detail(self) -> str | None:
        """Return why the last ``health_check`` failed, if it did.

        Read by ``mak.bootstrap.healthy_agent_ids`` so the startup warning can
        name the actual cause — a wrong base URL, an expired key, a quota
        breach — instead of guessing at "missing key/SDK, or CLI not on PATH",
        which is never the reason a running server refused a request.
        """
        return self._health_detail

    def health_status(self) -> str:
        """Return a human phrase for the last check: probed, or merely valid."""
        if self._health_detail is not None:
            return self._health_detail
        return NOT_PROBED if self._not_probed else "healthy"


def _probe_chat(client: Any, model: str) -> None:
    """Send the smallest possible real completion, to prove the model answers.

    Billed, so it runs only behind an explicit acceptance. Deliberately *not*
    structured: this asks "does this model respond at all", and a server that
    rejects ``response_format`` would otherwise fail a probe it should pass.
    """
    try:
        probe = client.with_options(timeout=15.0)
    except AttributeError:
        probe = client
    probe.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=1,
    )


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


# The name this class carried while it served only OpenAI itself, before it
# generalized to every OpenAI-compatible endpoint. Kept so existing imports —
# including the composition root's adapter table and third-party code — keep
# working.
OpenAiApiAdapter = OpenAiCompatibleAdapter
