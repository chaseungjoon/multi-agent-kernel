"""Tests for mak.bootstrap: the config → collaborators composition root."""

from __future__ import annotations

import pytest

from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
from mak.agent_runner.adapters.claude_code_adapter import ClaudeCodeAdapter
from mak.agent_runner.adapters.copilot_adapter import CopilotAdapter
from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter
from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.agent_runner.sandbox import SandboxConfig
from mak.bootstrap import (
    agents_from_specs,
    build_registry,
    default_agent_type,
    validate_config,
)
from mak.config import AgentConfig, MakConfig
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
        with pytest.raises(ConfigError, match="unknown provider 'mistral'"):
            agents_from_specs(["mistral"])

    def test_duplicate_provider_raises(self) -> None:
        with pytest.raises(ConfigError, match="more than once"):
            agents_from_specs(["anthropic", "anthropic:claude-opus-4-8"])

    def test_empty_specs_raises(self) -> None:
        with pytest.raises(ConfigError):
            agents_from_specs([])

    def test_roster_builds_a_registry(self) -> None:
        registry = build_registry(_config(*agents_from_specs(["anthropic", "openai"])))
        assert set(registry.list_types()) == {"anthropic_api", "openai_api"}


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
        assert adapter.command == ["my-claude"]

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
