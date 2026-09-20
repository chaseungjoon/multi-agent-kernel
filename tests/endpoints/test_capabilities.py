"""Tests for the session capability cache and the structured-output ladder."""

from __future__ import annotations

import threading

import pytest

from mak.endpoints.capabilities import (
    STRUCTURED_OUTPUT_LADDER,
    CapabilityCache,
    rungs_from,
)


class TestLadder:
    def test_the_ladder_descends_strongest_to_weakest(self) -> None:
        assert STRUCTURED_OUTPUT_LADDER == ("json_schema", "json_object", "none")

    def test_the_top_rung_can_reach_the_bottom(self) -> None:
        """The pre-Wave-22 single downgrade made this unreachable."""
        assert rungs_from("json_schema") == ("json_schema", "json_object", "none")

    def test_a_middle_rung_never_climbs(self) -> None:
        assert rungs_from("json_object") == ("json_object", "none")

    def test_the_bottom_rung_has_nowhere_to_go(self) -> None:
        assert rungs_from("none") == ("none",)

    def test_an_unknown_mode_is_its_own_only_rung(self) -> None:
        assert rungs_from("something-else") == ("something-else",)


class TestCache:
    def test_an_untried_pair_is_unknown(self) -> None:
        assert CapabilityCache().structured_output("gw", "m") is None

    def test_a_recorded_mode_reads_back(self) -> None:
        cache = CapabilityCache()
        cache.record_structured_output("gw", "m", "json_object")
        assert cache.structured_output("gw", "m") == "json_object"

    def test_the_key_is_the_pair_not_the_model(self) -> None:
        """Two services offering one model id are two implementations."""
        cache = CapabilityCache()
        cache.record_structured_output("openrouter", "claude-opus-5", "none")
        assert cache.structured_output("anthropic", "claude-opus-5") is None

    def test_a_later_record_supersedes(self) -> None:
        cache = CapabilityCache()
        cache.record_structured_output("gw", "m", "json_schema")
        cache.record_structured_output("gw", "m", "none")
        assert cache.structured_output("gw", "m") == "none"

    def test_the_snapshot_is_a_copy(self) -> None:
        cache = CapabilityCache()
        cache.record_structured_output("gw", "m", "none")
        snapshot = cache.snapshot()
        snapshot.clear()
        assert cache.structured_output("gw", "m") == "none"

    def test_two_caches_share_nothing(self) -> None:
        """Not a module global: two sessions in one process stay independent."""
        first, second = CapabilityCache(), CapabilityCache()
        first.record_structured_output("gw", "m", "none")
        assert second.structured_output("gw", "m") is None


class TestAnnouncement:
    def test_only_the_first_call_announces(self) -> None:
        cache = CapabilityCache()
        assert [cache.should_announce("gw", "m") for _ in range(3)] == [
            True,
            False,
            False,
        ]

    def test_each_pair_announces_separately(self) -> None:
        cache = CapabilityCache()
        assert cache.should_announce("gw", "a") is True
        assert cache.should_announce("gw", "b") is True

    def test_concurrent_announcers_still_announce_once(self) -> None:
        """Claiming and testing are one atomic step under the lock."""
        cache = CapabilityCache()
        results: list[bool] = []
        barrier = threading.Barrier(8)
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            got = cache.should_announce("gw", "m")
            with lock:
                results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results.count(True) == 1


class TestThreadSafety:
    def test_concurrent_writes_do_not_lose_entries(self) -> None:
        """Agents dispatch from scheduler threads; every path takes the lock."""
        cache = CapabilityCache()
        barrier = threading.Barrier(16)

        def worker(index: int) -> None:
            barrier.wait()
            cache.record_structured_output(f"gw{index}", "m", "json_object")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(cache.snapshot()) == 16

    def test_reads_during_writes_never_raise(self) -> None:
        cache = CapabilityCache()
        stop = threading.Event()
        errors: list[BaseException] = []

        def reader() -> None:
            try:
                while not stop.is_set():
                    cache.structured_output("gw", "m")
                    cache.snapshot()
            except BaseException as exc:  # noqa: BLE001 - recorded, then asserted
                errors.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for index in range(500):
                cache.record_structured_output("gw", f"m{index}", "none")
        finally:
            stop.set()
            thread.join(timeout=5)
        assert errors == []


def test_the_adapter_and_the_endpoint_enum_agree_on_the_mode_names() -> None:
    """The adapter duplicates these as plain strings; they must not drift."""
    from mak.agent_runner.adapters import openai_api_adapter as adapter
    from mak.endpoints.types import StructuredOutput, TokenParameter

    assert set(STRUCTURED_OUTPUT_LADDER) <= {m.value for m in StructuredOutput}
    assert adapter.TOKEN_PARAM_AUTO == TokenParameter.AUTO.value
    assert adapter.TOKEN_PARAM_NONE == TokenParameter.NONE.value
    assert adapter.TOKEN_PARAM_MAX_TOKENS == TokenParameter.MAX_TOKENS.value
    assert (
        adapter.TOKEN_PARAM_MAX_COMPLETION_TOKENS
        == TokenParameter.MAX_COMPLETION_TOKENS.value
    )


def test_a_registry_gives_every_agent_the_same_cache() -> None:
    """One memory per run: what one agent learns, the next one starts from."""
    from mak.bootstrap import build_registry
    from mak.config import AgentConfig, MakConfig

    config = MakConfig(
        agents=(
            AgentConfig(type="openai_api", id="a", model="m"),
            AgentConfig(type="openai_api", id="b", model="m"),
        )
    )
    registry = build_registry(config)
    first = registry.get("a")
    second = registry.get("b")
    assert first._capabilities is second._capabilities  # type: ignore[attr-defined]


def test_two_registries_do_not_share_a_cache() -> None:
    from mak.bootstrap import build_registry
    from mak.config import AgentConfig, MakConfig

    config = MakConfig(agents=(AgentConfig(type="openai_api", model="m"),))
    one = build_registry(config).get("openai_api")
    two = build_registry(config).get("openai_api")
    assert one._capabilities is not two._capabilities  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", ["json_schema", "json_object", "none"])
def test_every_ladder_rung_is_a_valid_starting_point(mode: str) -> None:
    assert rungs_from(mode)[0] == mode
