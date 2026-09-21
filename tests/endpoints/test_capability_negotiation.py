"""Wave 24: catalog-driven rung selection and single-flight discovery.

Two behaviours are proven here, both measured against the live OpenRouter API
before being written down (see ``CUR_WAVE.md`` for the probe transcripts):

* which rung a *reported* parameter set authorizes — and, just as importantly,
  which sets authorize nothing at all and must leave the ladder alone;
* that concurrent agents share one discovery instead of each paying for the
  same rejected requests.

The concurrency tests use barriers and events, never sleeps. A sleep-based
concurrency test either passes for the wrong reason on a fast machine or flakes
on a slow one, and neither outcome tells you the lock is right.
"""

from __future__ import annotations

import threading

import pytest

from mak.endpoints.capabilities import (
    RUNG_PARAMETER,
    STRUCTURED_OUTPUT_LADDER,
    CapabilityCache,
    rung_is_reported,
    start_rung_for,
)

# The two live reports the whole design rests on. Verbatim from
# ``GET /api/v1/models`` on 2026-09-21.
LING_FREE = frozenset(
    {
        "frequency_penalty", "include_reasoning", "logprobs", "max_tokens",
        "presence_penalty", "reasoning", "repetition_penalty", "seed", "stop",
        "temperature", "tool_choice", "tools", "top_k", "top_logprobs", "top_p",
    }
)
LING_PAID = LING_FREE | {"response_format", "structured_outputs", "logit_bias"}
# Reports response_format but NOT structured_outputs. 30 of 446 models sit here.
GEMMA_FREE = frozenset(
    {
        "frequency_penalty", "max_tokens", "presence_penalty", "reasoning",
        "response_format", "stop", "temperature", "tool_choice", "tools",
    }
)


class TestStartRung:
    """Which rung a reported parameter set authorizes as a starting point."""

    def test_the_incident_model_starts_at_prompt_only(self) -> None:
        """``ling-3.0-flash-vl:free`` reports neither parameter.

        This single assertion is the wave's headline: the fact was published by
        OpenRouter all along, and reading it turns three failing calls per task
        into one working call.
        """
        assert start_rung_for(LING_FREE) == "none"

    def test_the_paid_sibling_is_unrestricted(self) -> None:
        """Same model family, opposite capabilities. The suffix is significant."""
        assert start_rung_for(LING_PAID) is None

    def test_response_format_without_structured_outputs_caps_at_json_object(
        self,
    ) -> None:
        """The refinement the live probe forced.

        Gating both rungs on ``response_format`` — the obvious reading — would
        keep sending these 30 models a strict schema they cannot honor, one
        wasted call per task. ``json_schema`` needs ``structured_outputs``, and
        the two names are not synonyms.
        """
        assert start_rung_for(GEMMA_FREE) == "json_object"

    def test_an_unreported_set_authorizes_nothing(self) -> None:
        """Most compatible ``/models`` routes return bare ids.

        Reading that silence as "no structured output" would disable schema
        enforcement for every one of them.
        """
        assert start_rung_for(None) is None

    def test_an_empty_report_is_unknown_not_unsupported(self) -> None:
        """``openrouter/fusion`` publishes ``[]`` and *works* with json_schema.

        Three models in the live catalog do this — all auto-routers. An empty
        list is a service declining to enumerate, not one declaring nothing
        works, and probing confirmed the difference matters.
        """
        assert start_rung_for(frozenset()) is None

    def test_structured_outputs_alone_is_unrestricted(self) -> None:
        """15 live models report the schema parameter and not the object one.

        Odd, but the ladder handles it: start at the top and descend reactively
        if the middle rung turns out to be missing too.
        """
        assert start_rung_for(frozenset({"structured_outputs"})) is None

    def test_every_returned_rung_is_on_the_ladder(self) -> None:
        """A seed that is not a rung could never be descended from."""
        for report in (LING_FREE, LING_PAID, GEMMA_FREE, frozenset(), None):
            rung = start_rung_for(report)
            assert rung is None or rung in STRUCTURED_OUTPUT_LADDER


class TestRungIsReported:
    """What gates the routing guard: positive confirmation, nothing weaker."""

    def test_a_confirmed_rung_is_reported(self) -> None:
        assert rung_is_reported("json_object", GEMMA_FREE) is True

    def test_an_unconfirmed_rung_is_not(self) -> None:
        """Gemma's free route publishes no ``structured_outputs``."""
        assert rung_is_reported("json_schema", GEMMA_FREE) is False

    @pytest.mark.parametrize("report", [None, frozenset()])
    def test_unknown_and_empty_never_confirm(
        self, report: frozenset[str] | None
    ) -> None:
        """The guard must not be sent on a guess: its failure is a 404.

        Outside the 400/422 window a format rejection lives in, so a guard sent
        speculatively converts a recoverable provider refusal into a routing
        failure the ladder cannot descend from.
        """
        assert rung_is_reported("json_schema", report) is False
        assert rung_is_reported("json_object", report) is False

    def test_the_prompt_only_rung_is_never_guarded(self) -> None:
        """It sends no ``response_format``, so there is nothing to require."""
        assert rung_is_reported("none", LING_PAID) is False

    def test_the_rung_table_covers_both_structured_rungs(self) -> None:
        """Pins the mapping against the ladder, so a new rung cannot be silent."""
        assert set(RUNG_PARAMETER) == {"json_schema", "json_object"}
        assert RUNG_PARAMETER["json_schema"] == "structured_outputs"
        assert RUNG_PARAMETER["json_object"] == "response_format"


class TestReportedParameters:
    """Seeding preserves the tri-state and keeps variants apart."""

    def test_a_reported_set_is_readable_back(self) -> None:
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", "m:free", LING_FREE)
        assert cache.reported_parameters("openrouter", "m:free") == LING_FREE

    def test_recording_none_records_nothing(self) -> None:
        """Unknown must stay unknown, not become an empty report."""
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", "m", None)
        assert cache.reported_parameters("openrouter", "m") is None

    def test_an_empty_report_is_stored_as_empty(self) -> None:
        """Distinct from unknown on the way in as well as the way out."""
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", "m", [])
        assert cache.reported_parameters("openrouter", "m") == frozenset()

    def test_free_and_paid_variants_do_not_share_a_key(self) -> None:
        """The capability difference measured live is exactly between these two."""
        cache = CapabilityCache()
        cache.record_reported_parameters(
            "openrouter", "inclusionai/ling-3.0-flash-vl", LING_PAID
        )
        cache.record_reported_parameters(
            "openrouter", "inclusionai/ling-3.0-flash-vl:free", LING_FREE
        )
        paid = cache.reported_parameters(
            "openrouter", "inclusionai/ling-3.0-flash-vl"
        )
        free = cache.reported_parameters(
            "openrouter", "inclusionai/ling-3.0-flash-vl:free"
        )
        assert start_rung_for(paid) is None
        assert start_rung_for(free) == "none"

    def test_two_endpoints_offering_one_model_id_do_not_share_a_key(self) -> None:
        """The same id behind two services is two implementations."""
        cache = CapabilityCache()
        cache.record_reported_parameters("openrouter", "shared", LING_FREE)
        cache.record_reported_parameters("other", "shared", LING_PAID)
        assert cache.reported_parameters("other", "shared") == LING_PAID


class TestSingleFlightDiscovery:
    """One owner probes; the rest wait and then start from what it learned."""

    def test_the_first_caller_owns_discovery(self) -> None:
        cache = CapabilityCache()
        with cache.discovering("e", "m") as lease:
            assert lease.owned is True

    def test_a_proven_mode_needs_no_ownership(self) -> None:
        """Nothing left to discover, so nobody blocks anybody."""
        cache = CapabilityCache()
        cache.record_structured_output("e", "m", "json_object")
        with cache.discovering("e", "m") as lease:
            assert lease.owned is False
            assert lease.mode == "json_object"

    def test_the_key_is_released_on_success(self) -> None:
        cache = CapabilityCache()
        with cache.discovering("e", "m"):
            assert cache.in_flight() == 1
        assert cache.in_flight() == 0

    def test_the_key_is_released_on_exception(self) -> None:
        """A provider hiccup must not leave the key owned for the session."""
        cache = CapabilityCache()
        with pytest.raises(RuntimeError):
            with cache.discovering("e", "m"):
                raise RuntimeError("provider exploded")
        assert cache.in_flight() == 0

    def test_a_failed_owner_does_not_poison_the_key(self) -> None:
        """The next caller becomes the owner and tries for itself."""
        cache = CapabilityCache()
        with pytest.raises(RuntimeError):
            with cache.discovering("e", "m"):
                raise RuntimeError("boom")
        with cache.discovering("e", "m") as lease:
            assert lease.owned is True

    def test_four_agents_perform_one_discovery(self) -> None:
        """One discovery shared by four agents, not four failing ladders.

        Before this, four agents starting together each walked the same
        failing ladder, paying four times for one predictable mismatch.

        No sleeps, and the assertions hold under **every** interleaving rather
        than one the test had to arrange — which is what makes it trustworthy
        rather than merely green:

        * exactly one owner, because the owner records its result *before*
          releasing the key. A thread arriving while discovery is in flight
          waits and is handed the result; a thread arriving after it finished
          reads the proven mode. Neither can become a second owner.
        * every waiter resumes on that result, for the same reason.

        That waiters genuinely block (rather than sailing past) is proven
        separately by ``test_a_waiter_gives_up_rather_than_hanging``, which can
        only return by way of the bounded wait.
        """
        cache = CapabilityCache()
        owners: list[bool] = []
        modes: list[str | None] = []
        lock = threading.Lock()
        owner_entered = threading.Event()
        waiters_started = threading.Barrier(4)
        release_owner = threading.Event()

        def owner() -> None:
            with cache.discovering("e", "m") as lease:
                with lock:
                    owners.append(lease.owned)
                owner_entered.set()
                release_owner.wait(timeout=5)
                # Recorded inside the lease, which is what makes the result
                # visible to every waiter the moment the key is released.
                cache.record_structured_output("e", "m", "none")

        def waiter() -> None:
            waiters_started.wait(timeout=5)
            with cache.discovering("e", "m") as lease:
                with lock:
                    owners.append(lease.owned)
                    modes.append(lease.mode)

        first = threading.Thread(target=owner)
        first.start()
        assert owner_entered.wait(timeout=5), "the owner must enter its lease"

        rest = [threading.Thread(target=waiter) for _ in range(3)]
        for thread in rest:
            thread.start()
        # All three waiter threads are running and about to enter the lease.
        waiters_started.wait(timeout=5)
        release_owner.set()

        first.join(timeout=5)
        for thread in rest:
            thread.join(timeout=5)

        assert owners.count(True) == 1, "exactly one thread may own discovery"
        assert len(owners) == 4, "every agent must get a verdict"
        assert modes == ["none", "none", "none"], "waiters resume from the result"

    def test_waiters_are_released_when_the_owner_raises(self) -> None:
        """An owner that learns nothing must not strand its waiters."""
        cache = CapabilityCache()
        owner_entered = threading.Event()
        release_owner = threading.Event()
        waiter_done = threading.Event()

        def owner() -> None:
            try:
                with cache.discovering("e", "m"):
                    owner_entered.set()
                    release_owner.wait(timeout=5)
                    raise RuntimeError("provider exploded")
            except RuntimeError:
                pass

        def waiter() -> None:
            with cache.discovering("e", "m"):
                waiter_done.set()

        one = threading.Thread(target=owner)
        one.start()
        assert owner_entered.wait(timeout=5)
        two = threading.Thread(target=waiter)
        two.start()
        release_owner.set()
        one.join(timeout=5)
        two.join(timeout=5)
        assert waiter_done.is_set(), "the waiter must not block forever"

    def test_different_keys_discover_concurrently(self) -> None:
        """One slow model must not hold up an unrelated one.

        Both threads are required to be inside their leases at the same time;
        if the cache serialized on a global lock, the barrier would time out.
        """
        cache = CapabilityCache()
        both_inside = threading.Barrier(2)
        reached: list[str] = []
        lock = threading.Lock()

        def agent(model: str) -> None:
            with cache.discovering("e", model):
                both_inside.wait(timeout=5)
                with lock:
                    reached.append(model)

        threads = [
            threading.Thread(target=agent, args=(m,)) for m in ("m1", "m2")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert sorted(reached) == ["m1", "m2"]

    def test_a_waiter_gives_up_rather_than_hanging(self) -> None:
        """A wedged owner costs one agent its timeout, not the whole pool.

        The wait is bounded to near-zero here so the test is instant; in a real
        session the bound is generous enough that a normal ladder finishes well
        inside it.
        """
        cache = CapabilityCache(wait_seconds=0.01)
        owner_entered = threading.Event()
        release_owner = threading.Event()
        waiter_returned = threading.Event()

        def owner() -> None:
            with cache.discovering("e", "m"):
                owner_entered.set()
                release_owner.wait(timeout=5)

        def waiter() -> None:
            with cache.discovering("e", "m") as lease:
                assert lease.mode is None, "nothing was learned to adopt"
                waiter_returned.set()

        one = threading.Thread(target=owner)
        one.start()
        assert owner_entered.wait(timeout=5)
        two = threading.Thread(target=waiter)
        two.start()
        assert waiter_returned.wait(timeout=5), "a waiter must not hang"
        release_owner.set()
        one.join(timeout=5)
        two.join(timeout=5)

    def test_reported_parameters_reach_a_waiter(self) -> None:
        """A waiter that learned no mode still gets the catalog's claim."""
        cache = CapabilityCache(wait_seconds=0.01)
        cache.record_reported_parameters("e", "m", LING_FREE)
        with cache.discovering("e", "m") as lease:
            assert lease.reported == LING_FREE

    def test_announcement_happens_once_across_threads(self) -> None:
        """Forty tasks report the discovery once, not forty times."""
        cache = CapabilityCache()
        results: list[bool] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def agent() -> None:
            start.wait(timeout=5)
            allowed = cache.should_announce("e", "m")
            with lock:
                results.append(allowed)

        threads = [threading.Thread(target=agent) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert results.count(True) == 1
