"""Tests for mak.bootstrap: the config → collaborators composition root."""

from __future__ import annotations

import pytest

from mak.agent_runner.adapters.anthropic_api_adapter import AnthropicApiAdapter
from mak.agent_runner.adapters.gemini_api_adapter import GeminiApiAdapter
from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.bootstrap import build_registry, default_agent_type
from mak.config import AgentConfig, MakConfig
from mak.core.exceptions import AgentError, ConfigError


def _config(*agents: AgentConfig) -> MakConfig:
    return MakConfig(agents=agents)


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

    def test_unimplemented_cli_type_validates_but_errors_on_use(self) -> None:
        registry = build_registry(
            _config(AgentConfig(type="claude_code", cmd="claude"))
        )
        assert "claude_code" in registry.list_types()
        with pytest.raises(AgentError, match="not yet implemented"):
            registry.get("claude_code")

    def test_empty_agents_raises(self) -> None:
        with pytest.raises(ConfigError, match="no agents"):
            build_registry(MakConfig(agents=()))


class TestDefaultAgentType:
    def test_first_agent_is_default(self) -> None:
        cfg = _config(
            AgentConfig(type="openai_api"), AgentConfig(type="anthropic_api")
        )
        assert default_agent_type(cfg) == "openai_api"

    def test_empty_agents_raises(self) -> None:
        with pytest.raises(ConfigError, match="no agents"):
            default_agent_type(MakConfig(agents=()))
