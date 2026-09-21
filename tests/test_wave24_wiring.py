"""Wave 24: the composition wiring — profile to resolution to seeded cache.

Two claims are structural rather than behavioural, and both are security- or
correctness-relevant enough to pin directly:

* the OpenRouter routing extension is **profile-driven**, so exactly one
  built-in preset carries it and every other endpoint resolves to ``none``;
* capability seeding happens at the composition root with an **injected**
  catalog view, so no adapter factory reads the disk and a test can state what
  the catalog says without writing a manifest.
"""

from __future__ import annotations

import pytest

from mak.bootstrap import build_registry, seed_capabilities
from mak.config import AgentConfig, MakConfig
from mak.endpoints.capabilities import CapabilityCache, start_rung_for
from mak.endpoints.profiles import BUILTIN_PROFILES, profile_for
from mak.endpoints.resolution import (
    ResolvedAgentConfig,
    ResolvedEndpoint,
    resolve_endpoint,
)
from mak.endpoints.types import (
    EndpointConfig,
    HealthPolicy,
    Location,
    ModelDiscovery,
    ProviderRouting,
    StructuredOutput,
    TokenParameter,
    Transport,
)
from mak.models.catalog import ModelEntry
from mak.models.registry import ReportedCapabilities

MODEL = "inclusionai/ling-3.0-flash-vl:free"
FREE_PARAMS = frozenset({"max_tokens", "tools", "temperature"})
PAID_PARAMS = FREE_PARAMS | {"response_format", "structured_outputs"}


def _endpoint(endpoint_id: str = "openrouter", **kwargs: object) -> ResolvedEndpoint:
    defaults: dict[str, object] = {
        "id": endpoint_id,
        "display_name": endpoint_id,
        "transport": Transport.OPENAI_CHAT,
        "base_url": "https://example.invalid/v1",
        "location": Location.HOSTED,
        "model_discovery": ModelDiscovery.MODELS,
        "health_check": HealthPolicy.MODELS,
        "structured_output": StructuredOutput.AUTO,
        "token_parameter": TokenParameter.MAX_TOKENS,
    }
    defaults.update(kwargs)
    return ResolvedEndpoint(**defaults)  # type: ignore[arg-type]


class TestProviderRoutingIsProfileDriven:
    """Exactly one preset claims the extension, and nothing infers it."""

    def test_only_openrouter_declares_it(self) -> None:
        """A list, not a spot check: a new preset cannot quietly opt in."""
        claiming = [
            p.id
            for p in BUILTIN_PROFILES
            if p.provider_routing is ProviderRouting.OPENROUTER
        ]
        assert claiming == ["openrouter"]

    def test_the_openrouter_preset_resolves_to_it(self) -> None:
        profile = profile_for("openrouter")
        assert profile is not None
        resolved = resolve_endpoint(profile.to_endpoint(), env={})
        assert resolved.provider_routing is ProviderRouting.OPENROUTER

    @pytest.mark.parametrize(
        "profile_id", ["nvidia", "deepseek", "zai-general", "zai-coding", "custom"]
    )
    def test_every_other_preset_resolves_to_none(self, profile_id: str) -> None:
        """An OpenRouter-only field on a DeepSeek or vLLM request is a bug."""
        profile = profile_for(profile_id)
        assert profile is not None
        resolved = resolve_endpoint(profile.to_endpoint(), env={})
        assert resolved.provider_routing is ProviderRouting.NONE

    def test_a_bare_endpoint_with_no_profile_resolves_to_none(self) -> None:
        """No transport default: a routing extension belongs to one service."""
        resolved = resolve_endpoint(
            EndpointConfig(id="mine", transport=Transport.OPENAI_CHAT), env={}
        )
        assert resolved.provider_routing is ProviderRouting.NONE

    def test_it_is_never_inferred_from_the_url(self) -> None:
        """A user may proxy OpenRouter, or point a custom endpoint at its host.

        In both cases a hostname check answers wrongly, which is why the policy
        is stated rather than sniffed.
        """
        resolved = resolve_endpoint(
            EndpointConfig(
                id="proxied",
                transport=Transport.OPENAI_CHAT,
                base_url="https://openrouter.ai/api/v1",
            ),
            env={},
        )
        assert resolved.provider_routing is ProviderRouting.NONE

    def test_an_endpoint_field_overrides_the_profile(self) -> None:
        """Precedence holds for this field like every other capability."""
        profile = profile_for("openrouter")
        assert profile is not None
        endpoint = profile.to_endpoint()
        resolved = resolve_endpoint(
            EndpointConfig(
                id=endpoint.id,
                transport=endpoint.transport,
                base_url=endpoint.base_url,
                profile="openrouter",
                provider_routing=ProviderRouting.NONE,
            ),
            env={},
        )
        assert resolved.provider_routing is ProviderRouting.NONE

    def test_the_adapter_receives_the_resolved_policy(self) -> None:
        """The value has to arrive at the transport, not merely be decided."""
        config = MakConfig(
            endpoints=(profile_for("openrouter").to_endpoint(),),  # type: ignore[union-attr]
            agents=(AgentConfig(type="", id="or", endpoint="openrouter", model=MODEL),),
        )
        adapter = build_registry(config).get("or")
        assert adapter.provider_routing == ProviderRouting.OPENROUTER.value  # type: ignore[attr-defined]

    def test_another_endpoint_receives_none(self) -> None:
        config = MakConfig(
            endpoints=(profile_for("nvidia").to_endpoint(),),  # type: ignore[union-attr]
            agents=(AgentConfig(type="", id="nv", endpoint="nvidia", model="m"),),
        )
        adapter = build_registry(config).get("nv")
        assert adapter.provider_routing == ProviderRouting.NONE.value  # type: ignore[attr-defined]


class TestSeeding:
    """What reaches the cache before the first dispatch, and what must not."""

    def test_a_reported_pair_is_seeded(self) -> None:
        cache = CapabilityCache()
        roster = (
            ResolvedAgentConfig(
                id="or",
                adapter_type="openai_api",
                endpoint=_endpoint(),
                model=MODEL,
            ),
        )
        lookup = ReportedCapabilities.from_entries(
            [
                ModelEntry(
                    provider="openrouter",
                    model_id=MODEL,
                    display_name="Ling",
                    supported_parameters=FREE_PARAMS,
                )
            ]
        )
        seed_capabilities(cache, roster, lookup)
        assert cache.reported_parameters("openrouter", MODEL) == FREE_PARAMS
        assert start_rung_for(cache.reported_parameters("openrouter", MODEL)) == (
            "none"
        )

    def test_no_mode_is_ever_seeded(self) -> None:
        """Only the *report* is seeded, never a proven mode.

        Choosing the rung is the adapter's job, because only the adapter knows
        the user's configured ceiling. Seeding a mode here would be able to
        push an agent above it.
        """
        cache = CapabilityCache()
        roster = (
            ResolvedAgentConfig(
                id="or",
                adapter_type="openai_api",
                endpoint=_endpoint(),
                model=MODEL,
            ),
        )
        seed_capabilities(
            cache,
            roster,
            ReportedCapabilities.from_entries(
                [
                    ModelEntry(
                        provider="openrouter",
                        model_id=MODEL,
                        display_name="Ling",
                        supported_parameters=FREE_PARAMS,
                    )
                ]
            ),
        )
        assert cache.structured_output("openrouter", MODEL) is None
        assert cache.snapshot() == {}

    def test_an_unknown_pair_is_not_recorded_as_empty(self) -> None:
        """"Unknown" and "reported nothing" are different facts.

        Recording the wrong one would disable structured output for every
        endpoint whose ``/models`` route returns bare ids.
        """
        cache = CapabilityCache()
        roster = (
            ResolvedAgentConfig(
                id="or",
                adapter_type="openai_api",
                endpoint=_endpoint(),
                model=MODEL,
            ),
        )
        seed_capabilities(cache, roster, ReportedCapabilities())
        assert cache.reported_parameters("openrouter", MODEL) is None

    def test_a_cli_agent_with_no_endpoint_is_skipped(self) -> None:
        """No URL, no credential, no capability negotiation to seed."""
        cache = CapabilityCache()
        roster = (
            ResolvedAgentConfig(id="cc", adapter_type="claude_code", model=None),
        )
        seed_capabilities(cache, roster, ReportedCapabilities())
        assert cache.reported_parameters("", "") is None

    def test_only_the_configured_pairs_are_seeded(self) -> None:
        """A 446-model catalog must not become 446 cache entries."""
        cache = CapabilityCache()
        roster = (
            ResolvedAgentConfig(
                id="or",
                adapter_type="openai_api",
                endpoint=_endpoint(),
                model=MODEL,
            ),
        )
        lookup = ReportedCapabilities.from_entries(
            [
                ModelEntry(
                    provider="openrouter",
                    model_id=MODEL,
                    display_name="a",
                    supported_parameters=FREE_PARAMS,
                ),
                ModelEntry(
                    provider="openrouter",
                    model_id="some/other-model",
                    display_name="b",
                    supported_parameters=PAID_PARAMS,
                ),
            ]
        )
        seed_capabilities(cache, roster, lookup)
        assert cache.reported_parameters("openrouter", "some/other-model") is None

    def test_build_registry_seeds_the_shared_cache(self) -> None:
        """The wiring, not just the helper: one call, one seeded cache."""
        config = MakConfig(
            endpoints=(profile_for("openrouter").to_endpoint(),),  # type: ignore[union-attr]
            agents=(AgentConfig(type="", id="or", endpoint="openrouter", model=MODEL),),
        )
        registry = build_registry(
            config,
            reported=ReportedCapabilities.from_entries(
                [
                    ModelEntry(
                        provider="openrouter",
                        model_id=MODEL,
                        display_name="Ling",
                        supported_parameters=FREE_PARAMS,
                    )
                ]
            ),
        )
        cache = registry.get("or")._capabilities  # type: ignore[attr-defined]
        assert cache is not None
        assert cache.reported_parameters("openrouter", MODEL) == FREE_PARAMS

    def test_omitting_the_catalog_keeps_the_historical_behaviour(self) -> None:
        """No seeding, every pair discovered at runtime — and no disk read."""
        config = MakConfig(
            endpoints=(profile_for("openrouter").to_endpoint(),),  # type: ignore[union-attr]
            agents=(AgentConfig(type="", id="or", endpoint="openrouter", model=MODEL),),
        )
        registry = build_registry(config)
        cache = registry.get("or")._capabilities  # type: ignore[attr-defined]
        assert cache is not None
        assert cache.reported_parameters("openrouter", MODEL) is None

    def test_agents_on_one_endpoint_share_the_seeded_cache(self) -> None:
        """Two models behind one endpoint, one cache, two independent keys."""
        config = MakConfig(
            endpoints=(profile_for("openrouter").to_endpoint(),),  # type: ignore[union-attr]
            agents=(
                AgentConfig(type="", id="free", endpoint="openrouter", model=MODEL),
                AgentConfig(
                    type="", id="paid", endpoint="openrouter", model="paid/model"
                ),
            ),
        )
        registry = build_registry(
            config,
            reported=ReportedCapabilities.from_entries(
                [
                    ModelEntry(
                        provider="openrouter",
                        model_id=MODEL,
                        display_name="free",
                        supported_parameters=FREE_PARAMS,
                    ),
                    ModelEntry(
                        provider="openrouter",
                        model_id="paid/model",
                        display_name="paid",
                        supported_parameters=PAID_PARAMS,
                    ),
                ]
            ),
        )
        free = registry.get("free")._capabilities  # type: ignore[attr-defined]
        paid = registry.get("paid")._capabilities  # type: ignore[attr-defined]
        assert free is paid, "one cache per registry"
        assert start_rung_for(free.reported_parameters("openrouter", MODEL)) == "none"
        assert (
            start_rung_for(free.reported_parameters("openrouter", "paid/model"))
            is None
        )
