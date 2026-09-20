"""Characterization tests taken **before** Wave 22 touches anything.

Wave 22 re-keys the adapter registry from agent *type* to agent *id* and moves
endpoint knowledge out of the adapters into an explicit endpoint model. That is
a wide refactor across the composition root, the session, the scheduler and the
whole CLI, and most of it is supposed to be invisible.

These tests pin the parts that must stay invisible, so a diff during the wave
separates intentional schema evolution from accidental regression. Where a
behaviour pinned here is *deliberately* changed by a later step, the test moves
to that step's own file and inverts — it never simply disappears.

Behaviour already covered elsewhere is not duplicated: the placeholder-key rule
lives in ``tests/agent_runner/test_openai_api_adapter.py``
(``TestLocalEndpoint``), planner backend resolution in
``tests/planner/test_llm.py`` (``TestBackendResolution``), and config discovery
in ``tests/test_config.py``.
"""

from __future__ import annotations

import os
from typing import Any

import cli.core.api_keys as api_keys
import pytest

from mak.agent_runner.registry import AdapterRegistry
from mak.bootstrap import (
    agents_from_specs,
    build_registry,
    default_agent_type,
    validate_config,
)
from mak.config import AgentConfig, MakConfig, user_config_dir
from mak.core.exceptions import ConfigError


def _config(*agents: AgentConfig) -> MakConfig:
    return MakConfig(agents=agents)


class TestRegistryIsKeyedByType:
    """The routing key, before and after Step 5 replaced type with agent id.

    For a legacy roster the derived id *is* the type, so the first and third
    tests here read identically on both sides of the change — which is the
    point: an existing config routes exactly as it always did.
    """

    def test_a_legacy_roster_registers_one_entry_per_type(self) -> None:
        registry = build_registry(
            _config(
                AgentConfig(type="anthropic_api"),
                AgentConfig(type="openai_api"),
                AgentConfig(type="gemini_api"),
            )
        )
        assert sorted(registry.list_types()) == [
            "anthropic_api",
            "gemini_api",
            "openai_api",
        ]

    def test_two_entries_of_one_type_are_now_refused_not_silently_merged(
        self,
    ) -> None:
        """Inverted by Step 5 — this is the bug the wave exists to fix.

        Before Wave 22 the second ``openai_api`` entry silently overwrote the
        first in the registry dict, so a roster naming two OpenAI-compatible
        endpoints ran only the last one and nothing said so. The collision is
        now an error naming the id, and the roster becomes legal the moment the
        two entries are given distinct ids.
        """
        with pytest.raises(ConfigError, match="resolve to the id 'openai_api'"):
            build_registry(
                _config(
                    AgentConfig(type="openai_api", model="first"),
                    AgentConfig(type="openai_api", model="second"),
                )
            )

    def test_the_same_roster_works_once_the_ids_differ(self) -> None:
        registry = build_registry(
            _config(
                AgentConfig(type="openai_api", id="cloud", model="first"),
                AgentConfig(type="openai_api", id="gateway", model="second"),
            )
        )
        assert registry.list_ids() == ["cloud", "gateway"]
        first: Any = registry.get("cloud")
        second: Any = registry.get("gateway")
        assert (first.model, second.model) == ("first", "second")

    def test_default_agent_type_is_the_first_configured_entry(self) -> None:
        config = _config(
            AgentConfig(type="gemini_api"), AgentConfig(type="openai_api")
        )
        assert default_agent_type(config) == "gemini_api"

    def test_register_factory_refuses_a_duplicate_id(self) -> None:
        """Inverted by Step 5: the bare dict assignment is gone.

        ``replace_factory`` is the deliberate door for swapping in a double;
        see ``tests/agent_runner/test_registry.py``.
        """
        registry = AdapterRegistry()
        make: Any = lambda: object()  # noqa: E731
        registry.register_factory("dup", make)
        with pytest.raises(ConfigError, match="two agents are configured"):
            registry.register_factory("dup", make)


class TestSpecParsing:
    """``--models`` grammar. Step 14 widens it; the shape must not drift."""

    def test_hosted_specs_carry_the_conventional_key_env(self) -> None:
        agents = agents_from_specs(["anthropic:claude-opus-5", "openai"])
        assert [a.api_key_env for a in agents] == [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        ]

    def test_a_model_id_keeps_every_colon_after_the_first(self) -> None:
        """Ollama tags and vendor slugs both depend on this rule."""
        (agent,) = agents_from_specs(["ollama:qwen2.5-coder:14b"])
        assert agent.model == "qwen2.5-coder:14b"

    def test_the_first_at_ends_the_body_so_userinfo_survives(self) -> None:
        (agent,) = agents_from_specs(["local:m@http://host:8000/v1"])
        assert agent.base_url == "http://host:8000/v1"

    def test_one_model_per_provider_is_todays_rule(self) -> None:
        """Deliberately reversed by Step 14 — uniqueness moves to the agent id."""
        with pytest.raises(ConfigError, match="more than once"):
            agents_from_specs(["openai:gpt-5.6-sol", "openai:gpt-5.5"])

    def test_base_url_is_refused_for_providers_that_would_ignore_it(self) -> None:
        with pytest.raises(ConfigError, match="does not take an"):
            agents_from_specs(["anthropic:claude-opus-5@http://h:1/v1"])


class TestLocalTransportValidation:
    """``validate_config``'s current rules, which Step 3 must keep honouring."""

    def test_local_api_without_a_base_url_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="requires a 'base_url'"):
            validate_config(_config(AgentConfig(type="local_api")))

    def test_base_url_on_a_hosted_type_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="ignores 'base_url'"):
            validate_config(
                _config(
                    AgentConfig(type="anthropic_api", base_url="http://h:1/v1")
                )
            )

    def test_an_ollama_only_option_elsewhere_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="ignores 'num_ctx'"):
            validate_config(
                _config(
                    AgentConfig(
                        type="local_api", base_url="http://h:1/v1", num_ctx=8192
                    )
                )
            )


class TestApiKeyStorage:
    """``cli.core.api_keys`` round trip. Step 10 generalizes the name set."""

    def test_saved_keys_round_trip_and_export(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        api_keys.save_keys({"ANTHROPIC_API_KEY": "sk-round-trip"})
        assert api_keys.load_keys()["ANTHROPIC_API_KEY"] == "sk-round-trip"
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-round-trip"

    def test_the_key_file_is_owner_only(self) -> None:
        api_keys.save_keys({"OPENAI_API_KEY": "sk-mode"})
        path = user_config_dir() / ".env"
        assert path.stat().st_mode & 0o777 == 0o600

    def test_an_exported_variable_beats_the_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api_keys.save_keys({"GEMINI_API_KEY": "from-file"})
        monkeypatch.setenv("GEMINI_API_KEY", "from-env")
        assert api_keys.load_keys()["GEMINI_API_KEY"] == "from-env"

    def test_only_the_known_names_are_written_today(self) -> None:
        """Pins the truncation Step 10 removes.

        ``save_keys`` renders the file from its fixed name tuple, so any line it
        does not know about — a comment, a per-endpoint credential — is lost on
        the next save. Step 10 replaces this with parse-merge-render and
        inverts this assertion.
        """
        path = user_config_dir() / ".env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# a comment\nNVIDIA_API_KEY=nv-secret\n", encoding="utf-8")
        api_keys.save_keys({"OPENAI_API_KEY": "sk-only"})
        body = path.read_text(encoding="utf-8")
        assert "NVIDIA_API_KEY" not in body
        assert "# a comment" not in body


class TestPlannerKeyInference:
    """``cli.runner`` infers the planner key from the model *name* today.

    Step 12 makes a selected endpoint authoritative instead. The prefix rules
    stay as the fallback for a bare model id, so they are pinned here.
    """

    @pytest.mark.parametrize(
        ("model", "env"),
        [
            ("claude-opus-5", "ANTHROPIC_API_KEY"),
            ("gemini-3-pro", "GEMINI_API_KEY"),
            ("gpt-5.6-sol", "OPENAI_API_KEY"),
        ],
    )
    def test_the_model_prefix_picks_the_key(self, model: str, env: str) -> None:
        from cli.core.state import CliState
        from cli.runner import _resolve_planner_api_key

        state = CliState(planner_model=model, api_keys={env: "sentinel"})
        assert _resolve_planner_api_key(state) == "sentinel"

    def test_a_local_planner_resolves_to_no_key(self) -> None:
        from cli.core.state import CliState
        from cli.runner import _resolve_planner_api_key

        state = CliState(
            planner_model="qwen2.5-coder:14b",
            planner_base_url="http://localhost:11434/v1",
            api_keys={"OPENAI_API_KEY": "sk-real"},
        )
        assert _resolve_planner_api_key(state) is None


class TestPackagedConfigLoads:
    """The shipped config must keep parsing through every schema change."""

    def test_the_packaged_default_is_valid(self) -> None:
        from mak.config import load_config, packaged_config_path

        config = load_config(packaged_config_path())
        validate_config(config)
        assert config.agents

    def test_every_packaged_example_is_valid(self) -> None:
        from mak.config import example_path, list_examples, load_config

        for name in list_examples():
            config = load_config(example_path(name))
            validate_config(config)
            assert config.agents, name


def test_user_config_dir_is_isolated_by_the_suite_fixture() -> None:
    """Guard: these tests must never read a developer's real ``~/.config/mak``."""
    assert "XDG_CONFIG_HOME" in os.environ
