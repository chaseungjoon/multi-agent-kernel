"""Tests for mak.bootstrap: the config → collaborators composition root."""

from __future__ import annotations

import pytest

from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
from mak.agent_runner.adapters.budget import resolve_agent_max_tokens
from mak.agent_runner.adapters.claude_code_adapter import ClaudeCodeAdapter
from mak.agent_runner.adapters.copilot_adapter import CopilotAdapter
from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter
from mak.agent_runner.adapters.ollama_api_adapter import OllamaApiAdapter
from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.agent_runner.registry import AdapterRegistry
from mak.agent_runner.sandbox import SandboxConfig
from mak.bootstrap import (
    agents_from_specs,
    build_registry,
    default_agent_type,
    healthy_agent_types,
    planner_from_spec,
    validate_config,
)
from mak.config import AgentConfig, MakConfig, PlannerConfig
from mak.core.exceptions import AgentError, ConfigError


def _config(*agents: AgentConfig) -> MakConfig:
    return MakConfig(agents=agents)


class TestAgentsFromSpecs:
    def test_maps_providers_to_adapter_types_and_keys(self) -> None:
        agents = agents_from_specs(["anthropic:claude-opus-4-8", "openai", "gemini"])
        assert [a.type for a in agents] == [
            "anthropic_api",
            "openai_api",
            "gemini_api",
        ]
        assert agents[0].model == "claude-opus-4-8"
        assert agents[1].model is None  # no model -> adapter default
        assert [a.api_key_env for a in agents] == [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
        ]

    def test_google_is_an_alias_for_gemini(self) -> None:
        (agent,) = agents_from_specs(["google:gemini-3-pro"])
        assert agent.type == "gemini_api"
        assert agent.model == "gemini-3-pro"

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(
            ConfigError, match="unknown endpoint or provider 'mistral'"
        ):
            agents_from_specs(["mistral"])

    def test_the_unknown_message_offers_the_endpoint_route(self) -> None:
        with pytest.raises(ConfigError, match="/endpoint add"):
            agents_from_specs(["mistral"])

    def test_a_repeated_provider_collides_on_its_derived_id(self) -> None:
        """Wave 22 moved uniqueness from provider to agent id.

        A legacy spec derives its id from its type, so repeating one still
        collides — but the message now names the id and says how to fix it,
        and two models on one *endpoint* are legal.
        """
        with pytest.raises(ConfigError, match="agent id 'anthropic_api'"):
            agents_from_specs(["anthropic", "anthropic:claude-opus-4-8"])

    def test_empty_specs_raises(self) -> None:
        with pytest.raises(ConfigError):
            agents_from_specs([])

    def test_roster_builds_a_registry(self) -> None:
        registry = build_registry(_config(*agents_from_specs(["anthropic", "openai"])))
        assert set(registry.list_types()) == {"anthropic_api", "openai_api"}


class TestPlannerFromSpec:
    """``--planner`` takes the ``--models`` grammar, with the model required."""

    def test_a_hosted_spec_names_backend_and_key(self) -> None:
        planner = planner_from_spec("anthropic:claude-opus-5", PlannerConfig())
        assert planner.model == "claude-opus-5"
        assert planner.backend == "anthropic"
        assert planner.api_key_env == "ANTHROPIC_API_KEY"
        assert planner.endpoint is None

    def test_every_route_field_from_the_config_is_replaced(self) -> None:
        stale = PlannerConfig(model="x", endpoint="nvidia", max_retries=7)
        planner = planner_from_spec("gemini:gemini-3.5-flash", stale)
        assert planner.endpoint is None
        assert planner.backend == "gemini"
        # Non-route settings are kept.
        assert planner.max_retries == 7

    def test_an_ollama_spec_keeps_its_tag_and_defaults_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAK_LOCAL_BASE_URL", raising=False)
        planner = planner_from_spec("ollama:qwen2.5-coder:14b", PlannerConfig())
        assert planner.model == "qwen2.5-coder:14b"
        assert planner.backend == "ollama"
        assert planner.base_url == "http://localhost:11434"

    def test_a_local_spec_takes_its_url(self) -> None:
        planner = planner_from_spec(
            "local:my-model@http://localhost:8000/v1", PlannerConfig()
        )
        assert planner.backend == "openai"
        assert planner.base_url == "http://localhost:8000/v1"
        assert planner.api_key_env is None

    @pytest.mark.parametrize(
        "spec",
        ["claude-opus-5", "anthropic", "anthropic:m@http://h/v1", "nope:model"],
    )
    def test_malformed_specs_are_refused(self, spec: str) -> None:
        with pytest.raises(ConfigError):
            planner_from_spec(spec, PlannerConfig())

    def test_the_command_line_accepts_it(self) -> None:
        from mak.__main__ import parse_args

        args = parse_args(["--task", "t", "--planner", "openai:gpt-5.6-sol"])
        assert args.planner == "openai:gpt-5.6-sol"

    def test_the_planner_key_follows_the_named_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mak.application import resolve_planner_key

        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        config = MakConfig(
            planner=PlannerConfig(model="claude-lookalike", backend="openai")
        )
        assert resolve_planner_key(config) == "sk-oai"


class TestBuildRegistry:
    def test_registers_every_configured_type(self) -> None:
        registry = build_registry(
            _config(
                AgentConfig(type="anthropic_api", model="claude-sonnet-4-6"),
                AgentConfig(type="openai_api", model="gpt-4o"),
            )
        )
        assert set(registry.list_types()) == {"anthropic_api", "openai_api"}

    def test_api_adapter_is_constructed_with_no_network_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No key in env: the lazy SDK client means building still succeeds.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        registry = build_registry(
            _config(
                AgentConfig(
                    type="anthropic_api",
                    model="claude-sonnet-4-6",
                    api_key_env="ANTHROPIC_API_KEY",
                )
            )
        )
        adapter = registry.get("anthropic_api")
        assert isinstance(adapter, AnthropicApiAdapter)
        assert adapter.model == "claude-sonnet-4-6"

    def test_configured_max_tokens_reaches_the_adapter(self) -> None:
        registry = build_registry(
            _config(AgentConfig(type="anthropic_api", max_tokens=12000))
        )
        adapter = registry.get("anthropic_api")
        assert isinstance(adapter, AnthropicApiAdapter)
        assert adapter.max_tokens == 12000

    def test_unset_max_tokens_leaves_the_adapter_to_resolve_it(self) -> None:
        # An unset knob must not arrive as an explicit None — the adapter's own
        # catalog lookup is the single place that decides the budget.
        registry = build_registry(
            _config(AgentConfig(type="anthropic_api", model="claude-sonnet-5"))
        )
        adapter = registry.get("anthropic_api")
        assert isinstance(adapter, AnthropicApiAdapter)
        assert adapter.max_tokens == resolve_agent_max_tokens("claude-sonnet-5")

    def test_configured_model_reaches_openai_adapter(self) -> None:
        registry = build_registry(_config(AgentConfig(type="openai_api", model="o3")))
        adapter = registry.get("openai_api")
        assert isinstance(adapter, OpenAiApiAdapter)
        assert adapter.model == "o3"

    def test_configured_model_reaches_gemini_adapter(self) -> None:
        registry = build_registry(
            _config(AgentConfig(type="gemini_api", model="gemini-3-pro"))
        )
        adapter = registry.get("gemini_api")
        assert isinstance(adapter, GeminiApiAdapter)
        assert adapter.model == "gemini-3-pro"

    def test_gemini_api_key_resolved_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "sk-gem")
        registry = build_registry(
            _config(AgentConfig(type="gemini_api", api_key_env="GEMINI_API_KEY"))
        )
        adapter = registry.get("gemini_api")
        assert isinstance(adapter, GeminiApiAdapter)
        assert adapter._api_key == "sk-gem"

    def test_api_key_resolved_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MY_KEY", "sk-test")
        registry = build_registry(
            _config(AgentConfig(type="anthropic_api", api_key_env="MY_KEY"))
        )
        adapter = registry.get("anthropic_api")
        assert isinstance(adapter, AnthropicApiAdapter)
        assert adapter._api_key == "sk-test"

    def test_cli_adapter_built_with_cmd_override(self) -> None:
        registry = build_registry(
            _config(AgentConfig(type="claude_code", cmd="my-claude"))
        )
        adapter = registry.get("claude_code")
        assert isinstance(adapter, ClaudeCodeAdapter)
        # cmd now selects the underlying binary the bridge wrapper drives.
        assert adapter.command[-2:] == ["--cli", "my-claude"]

    def test_cli_adapter_threads_sandbox(self) -> None:
        sandbox = SandboxConfig(image="busybox")
        registry = build_registry(
            _config(AgentConfig(type="copilot")), sandbox=sandbox
        )
        adapter = registry.get("copilot")
        assert isinstance(adapter, CopilotAdapter)
        # The configured sandbox is threaded in (its argv wrapping is used on spawn).
        assert adapter._sandbox is sandbox

    def test_unknown_type_errors_on_use(self) -> None:
        registry = build_registry(_config(AgentConfig(type="bogus_backend")))
        assert "bogus_backend" in registry.list_types()
        with pytest.raises(AgentError, match="not a known agent type"):
            registry.get("bogus_backend")

    def test_empty_agents_raises(self) -> None:
        with pytest.raises(ConfigError, match="no agents"):
            build_registry(MakConfig(agents=()))


class TestValidateConfig:
    def test_known_types_pass(self) -> None:
        validate_config(
            _config(
                AgentConfig(type="anthropic_api"),
                AgentConfig(type="claude_code"),
                AgentConfig(type="gemini_api"),
            )
        )  # does not raise

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ConfigError, match="unknown agent type"):
            validate_config(
                _config(AgentConfig(type="anthropic_api"), AgentConfig(type="typo"))
            )


class TestDefaultAgentType:
    def test_first_agent_is_default(self) -> None:
        cfg = _config(
            AgentConfig(type="openai_api"), AgentConfig(type="anthropic_api")
        )
        assert default_agent_type(cfg) == "openai_api"

    def test_empty_agents_raises(self) -> None:
        with pytest.raises(ConfigError, match="no agents"):
            default_agent_type(MakConfig(agents=()))


class TestHealthyAgentTypes:
    def _registry(self) -> AdapterRegistry:
        reg = AdapterRegistry()

        class Good:
            agent_type = "g"

            def health_check(self) -> bool:
                return True

        class Bad:
            agent_type = "b"

            def health_check(self) -> bool:
                return False

        class Boom:
            agent_type = "x"

            def health_check(self) -> bool:
                raise RuntimeError("no key")

        reg.register_factory("good", lambda: Good())  # type: ignore[arg-type,return-value]
        reg.register_factory("bad", lambda: Bad())  # type: ignore[arg-type,return-value]
        reg.register_factory("boom", lambda: Boom())  # type: ignore[arg-type,return-value]
        return reg

    def test_splits_healthy_from_unhealthy(self) -> None:
        healthy, unhealthy, _why = healthy_agent_types(
            self._registry(), ["good", "bad", "boom"]
        )
        assert healthy == ["good"]
        assert unhealthy == ["bad", "boom"]

    def test_preserves_order(self) -> None:
        healthy, _, _why = healthy_agent_types(self._registry(), ["bad", "good"])
        assert healthy == ["good"]

    def test_a_raised_failure_is_reported_as_its_own_reason(self) -> None:
        _healthy, _unhealthy, why = healthy_agent_types(self._registry(), ["boom"])
        assert "no key" in why["boom"]


class TestLocalEndpoints:
    """Wave 15.2: the local transports at the composition root."""

    def test_both_new_types_register_and_build(self) -> None:
        config = _config(
            AgentConfig(type="local_api", model="m", base_url="http://h:8000/v1"),
            AgentConfig(type="ollama_api", model="qwen2.5-coder:14b"),
        )
        registry = build_registry(config)
        local = registry.get("local_api")
        ollama = registry.get("ollama_api")
        assert isinstance(local, OpenAiApiAdapter)
        assert isinstance(ollama, OllamaApiAdapter)
        # D1's payoff: the shared class reports the type it was built under.
        assert local.agent_type == "local_api"
        assert ollama.agent_type == "ollama_api"

    def test_cloud_and_both_local_types_coexist_in_one_roster(self) -> None:
        # The whole reason ``local_api`` is a separate type: with one shared
        # type a run could have cloud OpenAI *or* a local model, never both.
        config = _config(
            AgentConfig(type="openai_api", model="gpt-5.6-sol"),
            AgentConfig(type="local_api", model="m", base_url="http://h:8000/v1"),
            AgentConfig(type="ollama_api", model="qwen2.5-coder:14b"),
        )
        registry = build_registry(config)
        types = {
            registry.get(t).agent_type
            for t in ("openai_api", "local_api", "ollama_api")
        }
        assert types == {"openai_api", "local_api", "ollama_api"}

    def test_every_option_reaches_the_local_adapter(self) -> None:
        config = _config(
            AgentConfig(
                type="local_api",
                model="m",
                base_url="http://h:8000/v1",
                structured_output="json_schema",
                repair_attempts=3,
            )
        )
        adapter = build_registry(config).get("local_api")
        assert isinstance(adapter, OpenAiApiAdapter)
        assert adapter.base_url == "http://h:8000/v1"
        assert adapter.structured_output == "json_schema"
        assert adapter.repair_attempts == 3

    def test_every_ollama_option_reaches_the_ollama_adapter(self) -> None:
        config = _config(
            AgentConfig(
                type="ollama_api",
                model="qwen2.5-coder:14b",
                num_ctx=16384,
                keep_alive="30m",
                temperature=0.1,
            )
        )
        adapter = build_registry(config).get("ollama_api")
        assert isinstance(adapter, OllamaApiAdapter)
        assert adapter.num_ctx == 16384
        assert adapter.keep_alive == "30m"
        assert adapter.temperature == 0.1
        # Defaulted, because the provider name is the runtime.
        assert adapter.base_url == "http://localhost:11434"

    def test_no_local_option_reaches_a_cloud_adapter(self) -> None:
        # The Anthropic/Gemini constructors take none of them; an unconditional
        # kwarg would be a TypeError at dispatch, not a config error at start.
        config = _config(AgentConfig(type="anthropic_api", model="claude-sonnet-5"))
        adapter = build_registry(config).get("anthropic_api")
        assert isinstance(adapter, AnthropicApiAdapter)
        assert not hasattr(adapter, "base_url")


class TestLocalSpecs:
    @pytest.mark.parametrize(
        "spec,expected_url",
        [
            ("ollama:qwen2.5-coder:14b", "http://localhost:11434"),
            ("ollama:llama3.1@http://box:11434", "http://box:11434"),
            ("ollama:llama3.1@http://box:11434/", "http://box:11434"),
        ],
    )
    def test_ollama_specs(self, spec: str, expected_url: str) -> None:
        (agent,) = agents_from_specs([spec])
        assert agent.type == "ollama_api"
        assert agent.base_url == expected_url
        assert agent.api_key_env is None

    def test_an_ollama_tag_keeps_its_colon(self) -> None:
        (agent,) = agents_from_specs(["ollama:qwen2.5-coder:14b"])
        assert agent.model == "qwen2.5-coder:14b"

    def test_local_spec_requires_an_explicit_url(self) -> None:
        (agent,) = agents_from_specs(["local:m@http://localhost:8000/v1"])
        assert agent.type == "local_api"
        assert agent.base_url == "http://localhost:8000/v1"
        assert agent.model == "m"

    def test_local_without_a_url_is_rejected_showing_the_syntax(self) -> None:
        # Guessing Ollama's port for someone running vLLM is worse than asking.
        with pytest.raises(ConfigError, match="provider\\[:model\\]\\[@base_url\\]"):
            agents_from_specs(["local:m"])

    def test_a_local_spec_without_a_model_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="names no model"):
            agents_from_specs(["ollama"])

    def test_the_env_var_supplies_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAK_LOCAL_BASE_URL", "http://gpu-box:8000/v1")
        (agent,) = agents_from_specs(["local:m"])
        assert agent.base_url == "http://gpu-box:8000/v1"

    def test_an_explicit_url_beats_the_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAK_LOCAL_BASE_URL", "http://gpu-box:8000/v1")
        (agent,) = agents_from_specs(["local:m@http://other:9000/v1"])
        assert agent.base_url == "http://other:9000/v1"

    def test_a_userinfo_url_survives_the_split(self) -> None:
        # Split on the FIRST '@': rpartition would cut inside the userinfo.
        (agent,) = agents_from_specs(["local:m@http://user:pass@host:8000/v1"])
        assert agent.base_url == "http://user:pass@host:8000/v1"

    def test_openai_accepts_a_gateway_url(self) -> None:
        (agent,) = agents_from_specs(["openai:gpt-5.6-sol@https://gw.example/v1"])
        assert agent.type == "openai_api"
        assert agent.base_url == "https://gw.example/v1"
        assert agent.api_key_env == "OPENAI_API_KEY"

    @pytest.mark.parametrize("provider", ["anthropic", "gemini"])
    def test_anthropic_and_gemini_reject_a_url(self, provider: str) -> None:
        with pytest.raises(ConfigError, match="does not take an '@<base_url>'"):
            agents_from_specs([f"{provider}:m@http://h/v1"])

    def test_a_malformed_url_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="http:// or https://"):
            agents_from_specs(["local:m@ftp://host:8000"])

    def test_the_unknown_provider_message_lists_the_local_ones(self) -> None:
        with pytest.raises(ConfigError, match="ollama") as excinfo:
            agents_from_specs(["nope:m"])
        assert "local" in str(excinfo.value)

    def test_a_mixed_cloud_and_local_roster_builds(self) -> None:
        agents = agents_from_specs(
            ["anthropic:claude-opus-5", "ollama:qwen2.5-coder:14b"]
        )
        assert [a.type for a in agents] == ["anthropic_api", "ollama_api"]


class TestValidateLocalConfig:
    def test_base_url_on_a_type_that_ignores_it_is_rejected(self) -> None:
        config = _config(AgentConfig(type="anthropic_api", base_url="http://h/v1"))
        with pytest.raises(ConfigError, match="ignores 'base_url'"):
            validate_config(config)

    def test_local_api_without_a_base_url_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="localhost:11434/v1"):
            validate_config(_config(AgentConfig(type="local_api", model="m")))

    @pytest.mark.parametrize(
        "field,value", [("structured_output", "json_schema"), ("repair_attempts", 2)]
    )
    def test_structured_output_and_repair_outside_the_local_types(
        self, field: str, value: object
    ) -> None:
        config = _config(AgentConfig(type="gemini_api", **{field: value}))
        with pytest.raises(ConfigError, match=f"ignores '{field}'"):
            validate_config(config)

    @pytest.mark.parametrize(
        "field,value",
        [("num_ctx", 8192), ("keep_alive", "30m"), ("temperature", 0.1)],
    )
    def test_ollama_only_options_outside_ollama_api(
        self, field: str, value: object
    ) -> None:
        config = _config(
            AgentConfig(
                type="local_api", base_url="http://h/v1", **{field: value}
            )
        )
        with pytest.raises(ConfigError, match="ollama_api only"):
            validate_config(config)

    def test_a_valid_local_config_passes(self) -> None:
        validate_config(
            _config(
                AgentConfig(
                    type="local_api",
                    model="m",
                    base_url="http://h:8000/v1",
                    structured_output="json_schema",
                    repair_attempts=2,
                ),
                AgentConfig(
                    type="ollama_api",
                    model="qwen2.5-coder:14b",
                    num_ctx=16384,
                    keep_alive="30m",
                    temperature=0.1,
                ),
            )
        )


class TestHealthDetailReachesTheWarning:
    def test_an_adapters_own_reason_is_carried_back(self) -> None:
        class Detailed:
            agent_type = "ollama_api"

            def health_check(self) -> bool:
                return False

            def health_detail(self) -> str:
                return "Ollama is not running at http://localhost:11434"

        registry = AdapterRegistry()
        registry.register_factory("ollama_api", lambda: Detailed())  # type: ignore[arg-type,return-value]
        _healthy, unhealthy, why = healthy_agent_types(registry, ["ollama_api"])
        assert unhealthy == ["ollama_api"]
        assert why["ollama_api"] == "Ollama is not running at http://localhost:11434"

    def test_an_adapter_without_health_detail_is_not_required_to_have_one(
        self,
    ) -> None:
        class Plain:
            agent_type = "x"

            def health_check(self) -> bool:
                return False

        registry = AdapterRegistry()
        registry.register_factory("x", lambda: Plain())  # type: ignore[arg-type,return-value]
        _healthy, unhealthy, why = healthy_agent_types(registry, ["x"])
        assert unhealthy == ["x"]
        assert why == {}
