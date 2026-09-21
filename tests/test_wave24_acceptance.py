"""Wave 24 acceptance: the OpenRouter incident, end to end over real HTTP.

Every test here drives the **real** ``openai`` SDK against a loopback server
speaking OpenRouter's dialect — the nested ``error.metadata.raw`` envelope, the
``supported_parameters`` field on ``/models``, and the 404 routing body. A
mocked client cannot prove the things that actually broke: what lands in the
request body, whether the SDK surfaces a nested error where the classifier can
reach it, and whether an extension reaches an endpoint that never asked for it.

The scenario throughout is the one from the incident log:

.. code-block:: text

    endpoint: openrouter
    model:    inclusionai/ling-3.0-flash-vl:free
    provider: Novita   (does not implement response_format)
"""

from __future__ import annotations

import logging
import threading

import pytest

from mak.agent_runner.adapters.openai_api_adapter import (
    ROUTING_NONE,
    ROUTING_OPENROUTER,
    OpenAiCompatibleAdapter,
)
from mak.endpoints.capabilities import CapabilityCache
from mak.endpoints.types import ProviderRouting
from tests.support.fake_openai_server import Dialect, FakeOpenAiServer

openai = pytest.importorskip("openai")

# The base class every HTTP-status failure the SDK raises derives from, so a
# propagation test can say "the provider's own error reached the caller"
# without pinning which subclass each status maps to.
API_ERROR = openai.APIStatusError

MODEL = "inclusionai/ling-3.0-flash-vl:free"
PAID = "inclusionai/ling-3.0-flash-vl"

# Exactly what OpenRouter publishes for the free route: no response_format and
# no structured_outputs.
FREE_PARAMS = [
    "max_tokens", "temperature", "tools", "tool_choice", "top_p", "stop",
]
# The paid sibling publishes both.
PAID_PARAMS = [*FREE_PARAMS, "response_format", "structured_outputs"]
# The 30-model gap: object mode yes, strict schema no.
OBJECT_ONLY_PARAMS = [*FREE_PARAMS, "response_format"]


def _openrouter_dialect(**kwargs: object) -> Dialect:
    """Return a dialect behaving like OpenRouter in front of Novita."""
    defaults: dict[str, object] = {
        "models": [MODEL, PAID],
        "openrouter_errors": True,
        "require_parameters_404": True,
        "supported_parameters": {MODEL: FREE_PARAMS, PAID: PAID_PARAMS},
        # Novita implements neither structured rung for the free route.
        "reject_formats": frozenset({"json_schema", "json_object"}),
    }
    defaults.update(kwargs)
    return Dialect(**defaults)  # type: ignore[arg-type]


def _adapter(
    server: FakeOpenAiServer,
    *,
    model: str = MODEL,
    capabilities: CapabilityCache | None = None,
    routing: str = ROUTING_OPENROUTER,
    structured_output: str = "auto",
) -> OpenAiCompatibleAdapter:
    return OpenAiCompatibleAdapter(
        model=model,
        base_url=server.base_url,
        api_key="test-key",
        endpoint_id="openrouter",
        endpoint_name="OpenRouter",
        structured_output=structured_output,
        provider_routing=routing,
        capabilities=capabilities,
        timeout=10.0,
    )


class TestTheIncidentIsFixed:
    """The acceptance case: an agent task completes on the free Ling route."""

    def test_a_stale_catalog_still_recovers_within_one_dispatch(self) -> None:
        """No capability data at all: the reactive ladder must reach the bottom.

        This is the 0.8.1 failure reproduced over real HTTP. Novita refuses
        ``json_schema`` with one spelling and ``json_object`` with another; the
        literal marker matched only the first, so the ladder stalled and the
        task failed. All three rungs must now be tried, in order, and the last
        must succeed.
        """
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            adapter = _adapter(server, capabilities=CapabilityCache())
            assert adapter.parse_result(adapter.send("{}")).success is True
            formats = [r.response_format for r in server.chat_requests()]
            assert formats == ["json_schema", "json_object", ""]

    def test_a_fresh_catalog_prevents_the_bad_request_entirely(self) -> None:
        """The headline improvement: **one** call, and no ``response_format``.

        The endpoint published "I do not accept that parameter" in its own
        ``/models`` response. Seeding from it means MAK never sends a request
        the model has already refused in advance.
        """
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", MODEL, FREE_PARAMS)
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            adapter = _adapter(server, capabilities=cache)
            assert adapter.parse_result(adapter.send("{}")).success is True
            calls = server.chat_requests()
            assert len(calls) == 1, "a known-negative model needs no probing"
            assert "response_format" not in calls[0].body
            assert calls[0].provider_object == {}, "nothing to require"

    def test_the_prompt_still_demands_the_task_result_shape(self) -> None:
        """Dropping ``response_format`` must not drop the contract.

        Prompt-only JSON is only viable because the system prompt names every
        ``TaskResult`` field; losing that would trade a 400 for a parse error.
        """
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", MODEL, FREE_PARAMS)
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            adapter = _adapter(server, capabilities=cache)
            adapter.send("{}")
            system = server.chat_requests()[0].body["messages"][0]["content"]
            for key in (
                "task_id",
                "success",
                "modified_fragments",
                "no_changes_required",
                "error",
            ):
                assert key in system

    def test_the_discovered_rung_is_reused_by_the_next_task(self) -> None:
        """Forty tasks pay the discovery once, not forty times."""
        cache = CapabilityCache()
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            for _ in range(3):
                adapter = _adapter(server, capabilities=cache)
                assert adapter.parse_result(adapter.send("{}")).success is True
            formats = [r.response_format for r in server.chat_requests()]
            # Three rungs for the first task, then one call each.
            assert formats == ["json_schema", "json_object", "", "", ""]

    def test_the_free_suffix_is_not_canonicalized_away(self) -> None:
        """Two products, two capability sets, two cache keys.

        Stripping ``:free`` to "normalize" the id would attribute the paid
        route's structured-output support to the free one, which is the exact
        inversion of the truth.
        """
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", MODEL, FREE_PARAMS)
        cache.record_reported_parameters("openrouter", PAID, PAID_PARAMS)
        with FakeOpenAiServer(
            _openrouter_dialect(reject_formats=frozenset())
        ) as server:
            paid = _adapter(server, model=PAID, capabilities=cache)
            paid.send("{}")
            assert server.chat_requests()[-1].response_format == "json_schema"

            free = _adapter(server, model=MODEL, capabilities=cache)
            free.send("{}")
            assert server.chat_requests()[-1].response_format == ""


class TestCapableModelsKeepTheirSchema:
    """The fix must not weaken the endpoints that already worked."""

    def test_a_capable_model_keeps_strict_schema_and_gets_the_guard(self) -> None:
        """One call, schema intact, and routing pinned to a capable provider."""
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", PAID, PAID_PARAMS)
        with FakeOpenAiServer(
            _openrouter_dialect(reject_formats=frozenset())
        ) as server:
            adapter = _adapter(server, model=PAID, capabilities=cache)
            assert adapter.parse_result(adapter.send("{}")).success is True
            calls = server.chat_requests()
            assert len(calls) == 1
            assert calls[0].response_format == "json_schema"
            assert calls[0].require_parameters is True

    def test_an_object_only_model_starts_at_object_mode(self) -> None:
        """The 30-model gap the live probe uncovered.

        ``response_format`` and ``structured_outputs`` are different parameters.
        A model publishing only the former accepts ``{"type": "json_object"}``
        and refuses a strict schema, so starting at the top would waste a call
        per task forever — the catalog said so up front.
        """
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", MODEL, OBJECT_ONLY_PARAMS)
        with FakeOpenAiServer(
            _openrouter_dialect(
                reject_formats=frozenset({"json_schema"}),
                supported_parameters={MODEL: OBJECT_ONLY_PARAMS},
            )
        ) as server:
            adapter = _adapter(server, capabilities=cache)
            assert adapter.parse_result(adapter.send("{}")).success is True
            calls = server.chat_requests()
            assert len(calls) == 1
            assert calls[0].response_format == "json_object"
            assert calls[0].require_parameters is True

    def test_an_empty_report_keeps_schema_enforcement(self) -> None:
        """``openrouter/fusion`` publishes ``[]`` and serves schemas fine.

        Reading an empty list as "supports nothing" would silently downgrade
        every auto-router to prompt-only JSON.
        """
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", MODEL, [])
        with FakeOpenAiServer(
            _openrouter_dialect(
                reject_formats=frozenset(), require_parameters_404=False
            )
        ) as server:
            adapter = _adapter(server, capabilities=cache)
            adapter.send("{}")
            assert server.chat_requests()[0].response_format == "json_schema"


class TestTheRoutingGuard:
    """``provider.require_parameters``: where it goes, and where it must not."""

    def test_the_guard_never_reaches_another_endpoint(self) -> None:
        """An OpenRouter-only extension on a vLLM or NVIDIA request is a bug.

        Asserted on the **wire**, not on MAK's intent: the SDK folds
        ``extra_body`` into the top level of the JSON body, so that is the only
        place the claim can be checked honestly.
        """
        cache = CapabilityCache()
        cache.record_reported_parameters("generic", MODEL, PAID_PARAMS)
        with FakeOpenAiServer(
            Dialect(models=[MODEL], reject_formats=frozenset())
        ) as server:
            adapter = OpenAiCompatibleAdapter(
                model=MODEL,
                base_url=server.base_url,
                api_key="test-key",
                endpoint_id="generic",
                structured_output="auto",
                provider_routing=ROUTING_NONE,
                capabilities=cache,
                timeout=10.0,
            )
            adapter.send("{}")
            request = server.chat_requests()[0]
            assert request.response_format == "json_schema"
            assert "provider" not in request.body
            assert request.require_parameters is False

    def test_the_guard_is_absent_on_the_prompt_only_rung(self) -> None:
        """Nothing to require when no ``response_format`` is sent."""
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", MODEL, FREE_PARAMS)
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            adapter = _adapter(server, capabilities=cache)
            adapter.send("{}")
            assert "provider" not in server.chat_requests()[0].body

    def test_the_guard_is_absent_when_capability_is_unknown(self) -> None:
        """Sent on a guess it 404s, and a 404 is not a format rejection.

        That would turn a recoverable provider refusal into a hard failure the
        ladder cannot descend from — strictly worse than not sending it.
        """
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            adapter = _adapter(server, capabilities=CapabilityCache())
            adapter.send("{}")
            assert all(
                not r.require_parameters for r in server.chat_requests()
            ), "no guard may be sent for a model MAK knows nothing about"

    def test_a_stale_positive_report_recovers_by_dropping_the_guard(self) -> None:
        """Measured live: OpenRouter's aggregate can outrun its own routing.

        ``google/gemma-4-31b-it:free`` reported ``response_format`` while the
        route serving it did not, so the guard 404'd on a model that works. The
        recovery is to ask the same rung plainly, not to descend — descending
        would give up a capability the model actually has.
        """
        cache = CapabilityCache()
        # The catalog claims schema support; the server's routing disagrees.
        cache.record_reported_parameters("openrouter", MODEL, PAID_PARAMS)
        with FakeOpenAiServer(
            _openrouter_dialect(
                reject_formats=frozenset(),
                require_parameters_404=True,
                supported_parameters={MODEL: FREE_PARAMS},
            )
        ) as server:
            adapter = _adapter(server, capabilities=cache)
            assert adapter.parse_result(adapter.send("{}")).success is True
            calls = server.chat_requests()
            assert len(calls) == 2, "guarded attempt, then the same rung plainly"
            assert [c.response_format for c in calls] == [
                "json_schema",
                "json_schema",
            ], "the rung is retried, not abandoned"
            assert calls[0].require_parameters is True
            assert calls[1].require_parameters is False


class TestUnrelatedFailuresPropagate:
    """A weaker retry must never disguise a different problem."""

    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    def test_a_non_format_failure_never_descends_a_rung(
        self, status: int
    ) -> None:
        """Auth, quota and server errors are the provider's real answer.

        Asserted as "every attempt used the top rung" rather than "exactly one
        request": the openai SDK retries 429 and 5xx itself, which is its job
        and not MAK's ladder. What must never happen is a *descent* — that is
        what turned a clear 429 into a confusing second failure one rung down.
        """
        with FakeOpenAiServer(
            _openrouter_dialect(
                chat_status=status, reject_formats=frozenset()
            )
        ) as server:
            adapter = _adapter(server, capabilities=CapabilityCache())
            with pytest.raises(API_ERROR):
                adapter.send("{}")
            formats = {r.response_format for r in server.chat_requests()}
            assert formats == {"json_schema"}, "no rung below the top was tried"

    def test_a_credential_failure_is_not_answered_with_a_weaker_rung(self) -> None:
        """A 401 used to descend on any body mentioning the format."""
        with FakeOpenAiServer(
            _openrouter_dialect(require_auth="right-key")
        ) as server:
            adapter = _adapter(server, capabilities=CapabilityCache())
            with pytest.raises(API_ERROR):
                adapter.send("{}")
            formats = {r.response_format for r in server.chat_requests()}
            assert formats == {"json_schema"}, "a 401 must not descend"


class TestUserConfigurationIsRespected:
    """A ceiling the user set is never raised by catalog or peer discovery."""

    def test_a_named_mode_is_not_raised_by_a_positive_catalog(self) -> None:
        """``structured_output: json_object`` means "never a strict schema"."""
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", MODEL, PAID_PARAMS)
        with FakeOpenAiServer(
            _openrouter_dialect(
                reject_formats=frozenset(), require_parameters_404=False
            )
        ) as server:
            adapter = _adapter(
                server, capabilities=cache, structured_output="json_object"
            )
            adapter.send("{}")
            assert server.chat_requests()[0].response_format == "json_object"

    def test_an_explicit_none_stays_prompt_only(self) -> None:
        """The strongest statement a user can make about the reply shape."""
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", MODEL, PAID_PARAMS)
        with FakeOpenAiServer(
            _openrouter_dialect(
                reject_formats=frozenset(), require_parameters_404=False
            )
        ) as server:
            adapter = _adapter(
                server, capabilities=cache, structured_output="none"
            )
            adapter.send("{}")
            request = server.chat_requests()[0]
            assert "response_format" not in request.body
            assert "provider" not in request.body

    def test_a_peers_discovery_cannot_raise_this_agents_ceiling(self) -> None:
        """Two agents on one pair may have different configured ceilings.

        A mode proven by the permissive one must not be adopted by the
        restricted one if it sits above what that agent was configured for.
        """
        cache = CapabilityCache()
        cache.record_structured_output("openrouter", MODEL, "json_schema")
        with FakeOpenAiServer(
            _openrouter_dialect(
                reject_formats=frozenset(), require_parameters_404=False
            )
        ) as server:
            adapter = _adapter(
                server, capabilities=cache, structured_output="none"
            )
            adapter.send("{}")
            assert "response_format" not in server.chat_requests()[0].body


class TestConcurrentDispatch:
    """Four agents, one unknown pair, one shared discovery."""

    def test_four_concurrent_agents_probe_once(self) -> None:
        """Before Wave 24 this made twelve requests: four ladders of three.

        The server holds each completion open, so the four dispatches really do
        overlap. The assertion is on the *rejected* calls, which are what the
        single-flight lease exists to stop paying for repeatedly.
        """
        cache = CapabilityCache()
        with FakeOpenAiServer(
            _openrouter_dialect(delay_seconds=0.05)
        ) as server:
            errors: list[BaseException] = []
            lock = threading.Lock()
            start = threading.Barrier(4)

            def dispatch() -> None:
                start.wait(timeout=10)
                try:
                    adapter = _adapter(server, capabilities=cache)
                    assert adapter.parse_result(adapter.send("{}")).success
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    with lock:
                        errors.append(exc)

            threads = [threading.Thread(target=dispatch) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            assert not errors, f"every agent must succeed: {errors!r}"
            formats = [r.response_format for r in server.chat_requests()]
            rejected = [f for f in formats if f in {"json_schema", "json_object"}]
            assert rejected == ["json_schema", "json_object"], (
                "one shared ladder, not four: "
                f"{formats!r}"
            )
            assert formats.count("") == 4, "all four tasks completed"


class TestObservability:
    """One line per pair, naming the evidence, carrying nothing sensitive."""

    def test_a_runtime_discovery_is_logged_with_its_evidence(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cache = CapabilityCache()
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            with caplog.at_level(logging.INFO):
                adapter = _adapter(server, capabilities=cache)
                adapter.send("{}")
        messages = [r.getMessage() for r in caplog.records]
        selected = [m for m in messages if "reply format 'none'" in m]
        assert len(selected) == 1, f"expected one rung line, got {messages!r}"
        assert "runtime_rejection" in selected[0]
        assert MODEL in selected[0]

    def test_the_rung_line_appears_once_per_pair(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A forty-task run reports the discovery once."""
        cache = CapabilityCache()
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            with caplog.at_level(logging.INFO):
                for _ in range(3):
                    _adapter(server, capabilities=cache).send("{}")
        lines = [
            r.getMessage()
            for r in caplog.records
            if "for the rest of this session" in r.getMessage()
        ]
        assert len(lines) == 1

    def test_no_log_line_carries_a_key_header_or_prompt(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The security property: these lines end up in shared issue reports."""
        cache = CapabilityCache()
        secret = "sk-or-v1-supersecret"
        prompt = "TOPSECRETPROMPTBODY"
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            adapter = OpenAiCompatibleAdapter(
                model=MODEL,
                base_url=server.base_url,
                api_key=secret,
                endpoint_id="openrouter",
                endpoint_name="OpenRouter",
                structured_output="auto",
                provider_routing=ROUTING_OPENROUTER,
                capabilities=cache,
                timeout=10.0,
            )
            with caplog.at_level(logging.DEBUG):
                adapter.send(prompt)
        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in blob
        assert prompt not in blob
        assert "Authorization" not in blob
        assert "metadata" not in blob, "no raw provider body may be logged"


class TestContracts:
    """The adapter's plain-string mirrors must match the endpoint enums."""

    def test_routing_strings_match_the_enum(self) -> None:
        """Pin the adapter's plain strings to the endpoint enum.

        The adapter duplicates them so a bare test construction needs no
        endpoint import; this is the test that stops the two drifting apart.
        """
        assert ROUTING_NONE == ProviderRouting.NONE.value
        assert ROUTING_OPENROUTER == ProviderRouting.OPENROUTER.value
        assert {ROUTING_NONE, ROUTING_OPENROUTER} == {
            member.value for member in ProviderRouting
        }


class TestPromptOnlyQuality:
    """Prompt-only mode must still fail honestly when the model misbehaves.

    Dropping ``response_format`` also drops the server-side grammar, so a weak
    free model can and will return prose. The recoveries must stay bounded and
    an unrecoverable reply must surface as a protocol error — never as a task
    that quietly "succeeded" with no work in it.
    """

    def _cache(self) -> CapabilityCache:
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", MODEL, FREE_PARAMS)
        return cache

    def test_malformed_json_is_repaired_in_one_bounded_turn(self) -> None:
        """A follow-up turn, not a whole-bundle re-dispatch of tens of KB."""
        good = (
            '{"task_id": "t1", "success": true, "modified_fragments": [], '
            '"no_changes_required": true, "error": null}'
        )
        with FakeOpenAiServer(
            _openrouter_dialect(
                raw_contents=["Sure! Here is the result:", good]
            )
        ) as server:
            adapter = _adapter(server, capabilities=self._cache())
            assert adapter.parse_result(adapter.send("{}")).success is True
            calls = server.chat_requests()
            assert len(calls) == 2, "one prompt-only call, then one repair turn"
            assert all("response_format" not in c.body for c in calls)

    def test_a_persistently_invalid_reply_raises_a_protocol_error(self) -> None:
        """Never a silent success.

        ``AgentProtocolError`` specifically: the HTTP call worked and the model
        replied, so the fault is a malformed *body*, and only that
        classification reaches the session's schema-restating retry note.
        """
        from mak.core.exceptions import AgentProtocolError

        with FakeOpenAiServer(
            _openrouter_dialect(raw_contents=["not json at all"])
        ) as server:
            adapter = _adapter(server, capabilities=self._cache())
            with pytest.raises(AgentProtocolError):
                adapter.send("{}")
            assert len(server.chat_requests()) == 2, "the repair turn is bounded"

    def test_repair_is_disabled_when_configured_off(self) -> None:
        """``repair_attempts: 0`` means exactly one call and then the error."""
        from mak.core.exceptions import AgentProtocolError

        with FakeOpenAiServer(
            _openrouter_dialect(raw_contents=["nope"])
        ) as server:
            adapter = OpenAiCompatibleAdapter(
                model=MODEL,
                base_url=server.base_url,
                api_key="test-key",
                endpoint_id="openrouter",
                structured_output="auto",
                provider_routing=ROUTING_OPENROUTER,
                capabilities=self._cache(),
                repair_attempts=0,
                timeout=10.0,
            )
            with pytest.raises(AgentProtocolError):
                adapter.send("{}")
            assert len(server.chat_requests()) == 1

    def test_capability_discovery_costs_no_whole_task_retry(self) -> None:
        """The scheduler must not see a failed task because of negotiation.

        The incident amplified one predictable capability mismatch into
        repeated re-dispatches. Negotiation now lives entirely inside a single
        ``send``, so the caller sees one successful result.
        """
        with FakeOpenAiServer(_openrouter_dialect()) as server:
            adapter = _adapter(server, capabilities=CapabilityCache())
            result = adapter.parse_result(adapter.send("{}"))
            assert result.success is True
            assert result.task_id == "t1"
