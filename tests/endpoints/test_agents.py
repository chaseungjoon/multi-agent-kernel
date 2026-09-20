"""Tests for roster resolution: legacy parity, id derivation, coexistence.

The coexistence tests are the reason Wave 22 exists. Before it, two agents
sharing an adapter class collapsed into one registry entry and one of them
simply never ran.
"""

from __future__ import annotations

from typing import Any

import pytest

from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.bootstrap import agents_from_specs, build_registry
from mak.config import AgentConfig, MakConfig
from mak.core.exceptions import ConfigError
from mak.endpoints.agents import (
    derive_agent_id,
    resolve_agents,
    unique_agent_id,
)
from mak.endpoints.resolution import resolve_endpoints
from mak.endpoints.types import EndpointConfig, Location, Transport


def _endpoint(endpoint_id: str, url: str, key_env: str | None = None) -> EndpointConfig:
    return EndpointConfig(
        id=endpoint_id,
        transport=Transport.OPENAI_CHAT,
        base_url=url,
        api_key_env=key_env,
        location=Location.HOSTED,
    )


class TestLegacyParity:
    def test_a_type_based_roster_derives_todays_ids(self) -> None:
        config = MakConfig(
            agents=(
                AgentConfig(type="anthropic_api"),
                AgentConfig(type="openai_api"),
                AgentConfig(type="gemini_api"),
            )
        )
        resolved = resolve_agents(config, env={})
        assert [a.id for a in resolved] == [
            "anthropic_api",
            "openai_api",
            "gemini_api",
        ]
        assert [a.adapter_type for a in resolved] == [
            "anthropic_api",
            "openai_api",
            "gemini_api",
        ]

    def test_each_legacy_type_gets_its_builtin_endpoint(self) -> None:
        config = MakConfig(agents=(AgentConfig(type="anthropic_api"),))
        (agent,) = resolve_agents(config, env={"ANTHROPIC_API_KEY": "sk-a"})
        assert agent.endpoint is not None
        assert agent.endpoint.id == "anthropic"
        assert agent.endpoint.api_key == "sk-a"

    def test_local_api_resolves_as_local_not_hosted(self) -> None:
        config = MakConfig(
            agents=(
                AgentConfig(type="local_api", base_url="http://localhost:8000/v1"),
            )
        )
        (agent,) = resolve_agents(config, env={})
        assert agent.is_local
        assert agent.endpoint is not None
        assert agent.endpoint.location is Location.LOCAL

    def test_a_cli_agent_has_no_endpoint(self) -> None:
        config = MakConfig(agents=(AgentConfig(type="claude_code", cmd="claude"),))
        (agent,) = resolve_agents(config, env={})
        assert agent.endpoint is None
        assert agent.adapter_type == "claude_code"
        assert agent.cmd == "claude"

    def test_an_openai_agent_with_a_base_url_keeps_the_no_ambient_key_rule(
        self,
    ) -> None:
        """A gateway never inherits OPENAI_API_KEY unless the entry named it."""
        config = MakConfig(
            agents=(
                AgentConfig(type="openai_api", base_url="https://gateway/v1"),
            )
        )
        (agent,) = resolve_agents(config, env={"OPENAI_API_KEY": "sk-real"})
        assert agent.endpoint is not None
        assert agent.endpoint.api_key is None
        assert agent.endpoint.effective_key() == "local"

    def test_naming_the_variable_opts_back_in(self) -> None:
        config = MakConfig(
            agents=(
                AgentConfig(
                    type="openai_api",
                    base_url="https://gateway/v1",
                    api_key_env="OPENAI_API_KEY",
                ),
            )
        )
        (agent,) = resolve_agents(config, env={"OPENAI_API_KEY": "sk-real"})
        assert agent.endpoint is not None
        assert agent.endpoint.api_key == "sk-real"


class TestEndpointBackedAgents:
    def test_an_explicit_id_wins(self) -> None:
        config = MakConfig(
            endpoints=(_endpoint("nvidia", "https://nv/v1"),),
            agents=(
                AgentConfig(
                    type="", id="fast", endpoint="nvidia", model="meta/llama"
                ),
            ),
        )
        resolved = resolve_agents(
            config, endpoints=resolve_endpoints(config.endpoints, env={}), env={}
        )
        assert resolved[0].id == "fast"

    def test_an_id_is_derived_from_endpoint_and_model(self) -> None:
        config = MakConfig(
            endpoints=(_endpoint("nvidia", "https://nv/v1"),),
            agents=(
                AgentConfig(
                    type="", endpoint="nvidia", model="meta/llama-3.3-70b-instruct"
                ),
            ),
        )
        resolved = resolve_agents(
            config, endpoints=resolve_endpoints(config.endpoints, env={}), env={}
        )
        assert resolved[0].id == "nvidia-meta-llama-3-3-70b-instruct"

    def test_an_unknown_endpoint_is_an_error_naming_the_agent(self) -> None:
        config = MakConfig(
            agents=(AgentConfig(type="", endpoint="absent", model="m"),)
        )
        with pytest.raises(ConfigError, match="not configured"):
            resolve_agents(config, endpoints={}, env={})

    def test_the_transport_selects_the_adapter(self) -> None:
        config = MakConfig(
            endpoints=(_endpoint("nvidia", "https://nv/v1"),),
            agents=(AgentConfig(type="", endpoint="nvidia", model="m"),),
        )
        resolved = resolve_agents(
            config, endpoints=resolve_endpoints(config.endpoints, env={}), env={}
        )
        assert resolved[0].adapter_type == "openai_api"


class TestDuplicateIds:
    def test_two_agents_resolving_to_one_id_are_refused(self) -> None:
        config = MakConfig(
            agents=(
                AgentConfig(type="openai_api", model="a"),
                AgentConfig(type="openai_api", model="b"),
            )
        )
        with pytest.raises(ConfigError, match="resolve to the id 'openai_api'"):
            resolve_agents(config, env={})

    def test_the_message_says_how_to_fix_it(self) -> None:
        config = MakConfig(
            agents=(AgentConfig(type="openai_api"), AgentConfig(type="openai_api"))
        )
        with pytest.raises(ConfigError, match="give one of them an explicit 'id'"):
            resolve_agents(config, env={})


class TestThreeCompatibleEndpointsCoexist:
    """The wave's headline capability, end to end through the registry."""

    def _config(self) -> MakConfig:
        return MakConfig(
            endpoints=(
                _endpoint("nvidia", "https://nv/v1", "NVIDIA_API_KEY"),
                _endpoint("openrouter", "https://or/v1", "OPENROUTER_API_KEY"),
            ),
            agents=(
                AgentConfig(type="openai_api", model="gpt-5.6-sol", max_instances=1),
                AgentConfig(
                    type="",
                    id="nvidia-llama",
                    endpoint="nvidia",
                    model="meta/llama",
                    max_instances=2,
                ),
                AgentConfig(
                    type="",
                    id="openrouter-claude",
                    endpoint="openrouter",
                    model="anthropic/claude-opus-5",
                    max_instances=3,
                ),
            ),
        )

    def _resolve(self, env: dict[str, str] | None = None) -> Any:
        config = self._config()
        return resolve_agents(
            config,
            endpoints=resolve_endpoints(config.endpoints, env=env or {}),
            env=env or {},
        )

    def test_all_three_resolve_with_distinct_ids(self) -> None:
        assert [a.id for a in self._resolve()] == [
            "openai_api",
            "nvidia-llama",
            "openrouter-claude",
        ]

    def test_all_three_share_one_adapter_class(self) -> None:
        assert {a.adapter_type for a in self._resolve()} == {"openai_api"}

    def test_the_registry_keeps_all_three(self) -> None:
        """Before Wave 22 this collapsed to a single entry."""
        config = self._config()
        registry = build_registry(config, agents=self._resolve())
        assert registry.list_ids() == [
            "openai_api",
            "nvidia-llama",
            "openrouter-claude",
        ]

    def test_each_adapter_gets_its_own_url_model_and_key(self) -> None:
        env = {
            "OPENAI_API_KEY": "sk-openai",
            "NVIDIA_API_KEY": "sk-nvidia",
            "OPENROUTER_API_KEY": "sk-openrouter",
        }
        config = self._config()
        registry = build_registry(config, agents=self._resolve(env))
        cloud: Any = registry.get("openai_api")
        nvidia: Any = registry.get("nvidia-llama")
        router: Any = registry.get("openrouter-claude")
        assert isinstance(cloud, OpenAiApiAdapter)
        assert (cloud.model, nvidia.model, router.model) == (
            "gpt-5.6-sol",
            "meta/llama",
            "anthropic/claude-opus-5",
        )
        assert nvidia.base_url == "https://nv/v1"
        assert router.base_url == "https://or/v1"
        assert cloud.base_url is None

    def test_each_agent_reports_its_own_routing_id(self) -> None:
        config = self._config()
        registry = build_registry(config, agents=self._resolve())
        for agent_id in registry.list_ids():
            assert registry.get(agent_id).agent_id == agent_id

    def test_pool_caps_are_per_agent_not_per_transport(self) -> None:
        caps = {a.id: a.max_instances for a in self._resolve()}
        assert caps == {
            "openai_api": 1,
            "nvidia-llama": 2,
            "openrouter-claude": 3,
        }

    def test_the_planner_label_names_the_endpoint_but_no_url(self) -> None:
        labels = [a.label() for a in self._resolve()]
        assert "nvidia-llama — meta/llama via nvidia" in labels
        for label in labels:
            assert "https://" not in label
            assert "API_KEY" not in label


class TestDerivedIds:
    @pytest.mark.parametrize(
        ("endpoint", "model", "expected"),
        [
            (
                "nvidia",
                "meta/llama-3.3-70b-instruct",
                "nvidia-meta-llama-3-3-70b-instruct",
            ),
            ("or", "anthropic/claude-opus-5", "or-anthropic-claude-opus-5"),
            ("ds", "deepseek-chat", "ds-deepseek-chat"),
            ("ol", "qwen2.5-coder:14b", "ol-qwen2-5-coder-14b"),
            ("gw", "!!!", "gw"),
        ],
    )
    def test_derivation_is_deterministic_and_readable(
        self, endpoint: str, model: str, expected: str
    ) -> None:
        assert derive_agent_id(endpoint, model) == expected

    def test_a_long_model_id_is_truncated_without_a_trailing_dash(self) -> None:
        derived = derive_agent_id("gw", "x" * 200)
        assert len(derived) <= 3 + 48
        assert not derived.endswith("-")

    def test_a_collision_gets_a_numeric_suffix(self) -> None:
        taken = {"nvidia-m", "nvidia-m-2"}
        assert unique_agent_id("nvidia-m", taken) == "nvidia-m-3"

    def test_a_free_id_is_returned_unchanged(self) -> None:
        assert unique_agent_id("nvidia-m", set()) == "nvidia-m"


class TestSpecResolution:
    """Wave 22.13: ``--models`` resolves configured endpoints first."""

    def _save(self, *endpoints: EndpointConfig) -> None:
        from mak.endpoints.store import save_user_endpoints

        save_user_endpoints(endpoints)

    def test_an_endpoint_spec_builds_an_endpoint_backed_agent(self) -> None:
        from mak.bootstrap import agents_from_specs

        self._save(_endpoint("nvidia-work", "https://nv/v1", "NV_KEY"))
        (agent,) = agents_from_specs(["nvidia-work:meta/llama-3.3-70b-instruct"])
        assert agent.endpoint == "nvidia-work"
        assert agent.model == "meta/llama-3.3-70b-instruct"
        assert agent.id == "nvidia-work-meta-llama-3-3-70b-instruct"

    def test_two_models_on_one_endpoint_are_legal(self) -> None:
        """The restriction Wave 22 removes."""
        from mak.bootstrap import agents_from_specs

        self._save(_endpoint("nvidia-work", "https://nv/v1", "NV_KEY"))
        agents = agents_from_specs(
            ["nvidia-work:model-a", "nvidia-work:model-b"]
        )
        assert len(agents) == 2
        assert len({a.routing_id() for a in agents}) == 2

    def test_two_endpoints_on_one_transport_are_legal(self) -> None:
        from mak.bootstrap import agents_from_specs

        self._save(
            _endpoint("nvidia-work", "https://nv/v1", "NV_KEY"),
            _endpoint("openrouter-work", "https://or/v1", "OR_KEY"),
        )
        agents = agents_from_specs(
            ["nvidia-work:model-a", "openrouter-work:model-b"]
        )
        assert [a.endpoint for a in agents] == [
            "nvidia-work",
            "openrouter-work",
        ]

    def test_a_derived_id_collision_gets_a_suffix(self) -> None:
        """Two ids differing only in dropped characters must both stay usable."""
        from mak.bootstrap import agents_from_specs

        self._save(_endpoint("gw", "https://gw/v1", "GW_KEY"))
        agents = agents_from_specs(["gw:a/b", "gw:a-b"])
        ids = [a.routing_id() for a in agents]
        assert ids == ["gw-a-b", "gw-a-b-2"]

    def test_a_legacy_spec_still_works(self) -> None:
        from mak.bootstrap import agents_from_specs

        self._save(_endpoint("nvidia-work", "https://nv/v1", "NV_KEY"))
        (agent,) = agents_from_specs(["anthropic:claude-opus-5"])
        assert agent.type == "anthropic_api"
        assert agent.api_key_env == "ANTHROPIC_API_KEY"

    def test_a_legacy_base_url_spec_still_works(self) -> None:
        from mak.bootstrap import agents_from_specs

        (agent,) = agents_from_specs(["local:m@http://localhost:8000/v1"])
        assert agent.type == "local_api"
        assert agent.base_url == "http://localhost:8000/v1"

    def test_an_endpoint_spec_rejects_a_redundant_url(self) -> None:
        from mak.bootstrap import agents_from_specs

        self._save(_endpoint("gw", "https://gw/v1", "GW_KEY"))
        with pytest.raises(ConfigError, match="already has an address"):
            agents_from_specs(["gw:m@https://other/v1"])

    def test_an_endpoint_spec_with_no_model_says_how_to_list_them(self) -> None:
        from mak.bootstrap import agents_from_specs

        self._save(_endpoint("gw", "https://gw/v1", "GW_KEY"))
        with pytest.raises(ConfigError, match="/endpoint models gw"):
            agents_from_specs(["gw"])

    def test_the_model_id_keeps_every_colon_after_the_first(self) -> None:
        from mak.bootstrap import agents_from_specs

        self._save(_endpoint("gw", "https://gw/v1", "GW_KEY"))
        (agent,) = agents_from_specs(["gw:qwen2.5-coder:14b"])
        assert agent.model == "qwen2.5-coder:14b"

    def test_an_unreadable_store_does_not_break_legacy_specs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--models must keep working when the endpoint store is broken."""
        import mak.endpoints.store as store

        def boom(*_: object, **__: object) -> None:
            raise OSError("unreadable")

        monkeypatch.setattr(store, "load_user_endpoints", boom)
        (agent,) = agents_from_specs(["anthropic:claude-opus-5"])
        assert agent.type == "anthropic_api"
