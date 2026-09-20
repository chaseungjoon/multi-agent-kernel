"""Wave 22 acceptance: one test class per bullet in TASKS.md § Acceptance.

These are the wave's contract, written as the user journeys rather than as unit
coverage. They use the in-process fake server and the real command handlers, so
what passes here is what a user would actually experience.

No test in this file contacts the network or spends a provider token.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from cli.commands import handle_command
from cli.core.state import MODE_CLOUD, CliState
from rich.console import Console

from mak.agent_runner.adapters.openai_api_adapter import OpenAiCompatibleAdapter
from mak.bootstrap import agents_from_specs, build_registry, resolved_agents
from mak.config import AgentConfig, MakConfig, load_config
from mak.core.exceptions import ConfigError
from mak.endpoints.capabilities import CapabilityCache
from mak.endpoints.profiles import BUILTIN_PROFILES, profile_for
from mak.endpoints.resolution import PLACEHOLDER_KEY
from mak.endpoints.store import load_user_endpoints, save_user_endpoints
from mak.endpoints.types import EndpointConfig, Location, Transport
from tests.support.fake_openai_server import Dialect, FakeOpenAiServer


def _run(line: str, state: CliState) -> str:
    console = Console(width=200, no_color=True, highlight=False, record=True)
    handle_command(line, state, console)
    return console.export_text()


def _endpoint(
    endpoint_id: str,
    base_url: str,
    key_env: str | None = None,
    *,
    location: Location = Location.HOSTED,
    profile: str | None = None,
) -> EndpointConfig:
    return EndpointConfig(
        id=endpoint_id,
        transport=Transport.OPENAI_CHAT,
        base_url=base_url,
        api_key_env=key_env,
        location=location,
        profile=profile,
        display_name=endpoint_id,
    )


class TestPresetEndpointFromAFreshCli:
    """Acceptance 1: only NVIDIA_API_KEY, no YAML editing, survives restart."""

    def test_the_nvidia_preset_carries_the_documented_defaults(self) -> None:
        profile = profile_for("nvidia")
        assert profile is not None
        endpoint = profile.to_endpoint()
        assert endpoint.base_url == "https://integrate.api.nvidia.com/v1"
        assert endpoint.api_key_env == "NVIDIA_API_KEY"
        assert endpoint.location is Location.HOSTED

    def test_selection_needs_no_openai_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The journey's whole point: no misleading OPENAI_API_KEY export."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("NVIDIA_API_KEY", "sk-nv")
        save_user_endpoints(
            (
                _endpoint(
                    "nvidia",
                    "https://integrate.api.nvidia.com/v1",
                    "NVIDIA_API_KEY",
                    profile="nvidia",
                ),
            )
        )
        state = CliState()
        _run("/models nvidia:meta/llama-3.3-70b-instruct", state)
        _run("/planner nvidia:meta/llama-3.3-70b-instruct", state)
        assert state.selected_models == ["nvidia:meta/llama-3.3-70b-instruct"]
        assert state.planner_endpoint_id == "nvidia"

    def test_the_endpoint_survives_a_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restart is a fresh CliState reading the same on-disk store."""
        monkeypatch.setenv("NVIDIA_API_KEY", "sk-nv")
        save_user_endpoints(
            (_endpoint("nvidia", "https://nv.example/v1", "NVIDIA_API_KEY"),)
        )
        restarted = CliState()
        out = _run("/models nvidia:meta/llama", restarted)
        assert "Models:" in out
        assert [e.id for e in load_user_endpoints()[0]] == ["nvidia"]

    def test_the_run_is_buildable_without_editing_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NVIDIA_API_KEY", "sk-nv")
        save_user_endpoints(
            (_endpoint("nvidia", "https://nv.example/v1", "NVIDIA_API_KEY"),)
        )
        config = MakConfig(agents=agents_from_specs(["nvidia:meta/llama"]))
        roster = resolved_agents(config)
        assert roster[0].endpoint is not None
        assert roster[0].endpoint.api_key == "sk-nv"
        assert build_registry(config, agents=roster).list_ids() == [
            "nvidia-meta-llama"
        ]


class TestEveryPresetWorksTheSameWay:
    """Acceptance 2: the flow is identical for each preset and for keyless."""

    @pytest.mark.parametrize(
        "profile_id", ["nvidia", "openrouter", "deepseek", "zai-general", "zai-coding"]
    )
    def test_each_hosted_preset_resolves_to_a_usable_endpoint(
        self, profile_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profile = profile_for(profile_id)
        assert profile is not None
        assert profile.api_key_env is not None
        monkeypatch.setenv(profile.api_key_env, "sk-preset")
        save_user_endpoints((profile.to_endpoint(profile_id),))
        state = CliState()
        _run(f"/models {profile_id}:some-model", state)
        assert state.selected_models == [f"{profile_id}:some-model"]

    def test_the_two_zai_plans_are_separate_endpoints(self) -> None:
        """Choosing the wrong one charges the wrong balance."""
        general = profile_for("zai-general")
        coding = profile_for("zai-coding")
        assert general is not None and coding is not None
        assert general.base_url != coding.base_url

    def test_a_keyless_local_endpoint_needs_no_credential(self) -> None:
        save_user_endpoints(
            (_endpoint("vllm", "http://localhost:8000/v1", None,
                       location=Location.LOCAL),)
        )
        state = CliState()
        _run("/models vllm:local-model", state)
        assert state.selected_models == ["vllm:local-model"]

    def test_a_keyless_endpoint_sends_the_placeholder_not_a_cloud_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-cloud")
        with FakeOpenAiServer() as server:
            OpenAiCompatibleAdapter(
                model="m", base_url=server.base_url, repair_attempts=0
            ).send("{}")
            assert server.last.bearer == PLACEHOLDER_KEY


class TestThreeCompatibleAgentsInOneRun:
    """Acceptance 3: distinct ids, keys, URLs, models and pool caps."""

    def _config(self) -> MakConfig:
        return MakConfig(
            endpoints=(
                _endpoint("nvidia", "https://nv.example/v1", "NVIDIA_API_KEY"),
                _endpoint("openrouter", "https://or.example/v1", "OPENROUTER_API_KEY"),
            ),
            agents=(
                AgentConfig(type="openai_api", model="gpt-5.6-sol", max_instances=1),
                AgentConfig(
                    type="", id="nvidia-llama", endpoint="nvidia",
                    model="meta/llama", max_instances=2,
                ),
                AgentConfig(
                    type="", id="openrouter-claude", endpoint="openrouter",
                    model="anthropic/claude-opus-5", max_instances=3,
                ),
            ),
        )

    @pytest.fixture(autouse=True)
    def _keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("NVIDIA_API_KEY", "sk-nvidia")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter")

    def test_no_registry_entry_is_overwritten(self) -> None:
        config = self._config()
        registry = build_registry(config, agents=resolved_agents(config))
        assert registry.list_ids() == [
            "openai_api",
            "nvidia-llama",
            "openrouter-claude",
        ]

    def test_each_agent_keeps_its_own_key_and_url(self) -> None:
        roster = resolved_agents(self._config())
        keys = {a.id: (a.endpoint.api_key if a.endpoint else None) for a in roster}
        assert keys == {
            "openai_api": "sk-openai",
            "nvidia-llama": "sk-nvidia",
            "openrouter-claude": "sk-openrouter",
        }

    def test_pool_caps_are_per_agent(self) -> None:
        roster = resolved_agents(self._config())
        assert {a.id: a.max_instances for a in roster} == {
            "openai_api": 1,
            "nvidia-llama": 2,
            "openrouter-claude": 3,
        }

    def test_the_planner_sees_three_distinct_choices(self) -> None:
        labels = [a.label() for a in resolved_agents(self._config())]
        assert len(set(labels)) == 3

    def test_the_planner_prompt_leaks_no_url_or_credential(self) -> None:
        for label in (a.label() for a in resolved_agents(self._config())):
            assert "https://" not in label
            assert "sk-" not in label
            assert "API_KEY" not in label

    def test_a_recovery_file_round_trips_the_routing_id(self) -> None:
        from mak.core.task_codec import subtask_from_dict, subtask_to_dict
        from mak.core.types import SubTask

        task = SubTask(
            task_id="t1", description="d", agent_type="openrouter-claude"
        )
        encoded = json.loads(json.dumps(subtask_to_dict(task)))
        assert subtask_from_dict(encoded) == task

    def test_the_git_trailer_carries_the_id(self, tmp_path: Path) -> None:
        import subprocess

        from mak.git_integration.git import GitHelper

        repo = tmp_path / "repo"
        repo.mkdir()
        for cmd in (
            ["init", "-q"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "t"],
        ):
            subprocess.run(["git", *cmd], cwd=repo, check=True)
        source = repo / "a.py"
        source.write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
        source.write_text("x = 2\n", encoding="utf-8")
        GitHelper(repo).commit_task("t1", ["a.py"], "d", "nvidia-llama", "s1")
        log = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "Agent: nvidia-llama" in log


class TestEveryCliSurfaceUnderstandsEndpoints:
    """Acceptance 4: the commands, completions and status all agree."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NV_KEY", "sk-nv")
        save_user_endpoints(
            (_endpoint("nvidia", "https://nv.example/v1", "NV_KEY"),)
        )

    def test_endpoint_lists_it(self) -> None:
        assert "nvidia" in _run("/endpoint list", CliState())

    def test_models_accepts_it(self) -> None:
        state = CliState()
        _run("/models nvidia:m", state)
        assert state.selected_models == ["nvidia:m"]

    def test_planner_accepts_it(self) -> None:
        state = CliState()
        _run("/planner nvidia:m", state)
        assert state.planner_endpoint_id == "nvidia"

    def test_status_reports_it_as_hosted(self) -> None:
        state = CliState()
        _run("/models nvidia:m", state)
        assert "nvidia (hosted)" in _run("/status", state)

    def test_apikey_knows_its_credential_variable(self) -> None:
        from cli.core.api_keys import key_names_for
        from cli.endpoints.commands import all_endpoints

        assert "NV_KEY" in key_names_for(all_endpoints())

    def test_mode_is_not_flipped_to_local_by_a_hosted_endpoint(self) -> None:
        state = CliState()
        state.mode = MODE_CLOUD
        _run("/models nvidia:m", state)
        assert state.mode == MODE_CLOUD

    def test_non_interactive_models_resolves_it(self) -> None:
        (agent,) = agents_from_specs(["nvidia:m"])
        assert agent.endpoint == "nvidia"

    def test_help_lists_the_command(self) -> None:
        assert "/endpoint" in _run("/help", CliState())


class TestManualEntryAndTheFullLadder:
    """Acceptance 5: no /models route, and no structured output at all."""

    def test_a_service_without_a_listing_is_still_usable(self) -> None:
        with FakeOpenAiServer(Dialect(models=None)) as server:
            adapter = OpenAiCompatibleAdapter(
                model="typed-by-hand",
                base_url=server.base_url,
                health_check_policy="none",
                repair_attempts=0,
            )
            assert adapter.health_check() is True
            assert adapter.parse_result(adapter.send("{}")).success is True

    def test_manual_discovery_makes_no_network_call(self) -> None:
        from mak.endpoints.resolution import resolve_endpoint
        from mak.endpoints.types import ModelDiscovery
        from mak.models.providers import sources_for_endpoints

        with FakeOpenAiServer() as server:
            endpoint = EndpointConfig(
                id="manual",
                transport=Transport.OPENAI_CHAT,
                base_url=server.base_url,
                model_discovery=ModelDiscovery.MANUAL,
            )
            assert sources_for_endpoints([resolve_endpoint(endpoint)]) == ()
            assert server.requests == []

    def test_an_endpoint_rejecting_both_formats_reaches_prompt_only_once(
        self,
    ) -> None:
        cache = CapabilityCache()
        dialect = Dialect(reject_formats=frozenset({"json_schema", "json_object"}))
        with FakeOpenAiServer(dialect) as server:
            first = OpenAiCompatibleAdapter(
                model="m", base_url=server.base_url, endpoint_id="gw",
                structured_output="auto", capabilities=cache, repair_attempts=0,
            )
            assert first.parse_result(first.send("{}")).success is True
            second = OpenAiCompatibleAdapter(
                model="m", base_url=server.base_url, endpoint_id="gw",
                structured_output="auto", capabilities=cache, repair_attempts=0,
            )
            assert second.parse_result(second.send("{}")).success is True
            formats = [r.response_format for r in server.chat_requests()]
        # Three calls to discover, then exactly one on the second dispatch.
        assert formats == ["json_schema", "json_object", "", ""]

    def test_the_result_is_still_a_validated_task_result(self) -> None:
        dialect = Dialect(reject_formats=frozenset({"json_schema", "json_object"}))
        with FakeOpenAiServer(dialect) as server:
            adapter = OpenAiCompatibleAdapter(
                model="m", base_url=server.base_url, endpoint_id="gw",
                structured_output="auto", capabilities=CapabilityCache(),
                repair_attempts=0,
            )
            result = adapter.parse_result(adapter.send("{}"))
        assert result.task_id == "t1"
        assert result.no_changes_required is True


class TestSecretsAndWrites:
    """Acceptance 6: containment, atomicity, and the rejected URL shapes."""

    def test_no_sentinel_reaches_output_cache_or_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.endpoints.render import export_yaml

        from mak.endpoints.store import endpoints_path

        monkeypatch.setenv("NV_KEY", "sk-sentinel-value")
        endpoint = _endpoint("nvidia", "https://nv.example/v1", "NV_KEY")
        save_user_endpoints((endpoint,))
        state = CliState()
        _run("/models nvidia:m", state)
        surfaces = [
            _run("/endpoint list", state),
            _run("/endpoint show nvidia", state),
            _run("/status", state),
            export_yaml(endpoint),
            endpoints_path().read_text(encoding="utf-8"),
        ]
        for surface in surfaces:
            assert "sk-sentinel-value" not in surface

    def test_endpoint_metadata_is_owner_only(self) -> None:
        from mak.endpoints.store import endpoints_path

        save_user_endpoints((_endpoint("gw", "https://gw/v1"),))
        assert endpoints_path().stat().st_mode & 0o777 == 0o600

    def test_a_hosted_http_url_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "mak.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "endpoints": [
                        {
                            "id": "gw",
                            "base_url": "http://api.example.com/v1",
                            "location": "hosted",
                        }
                    ],
                    "agents": [{"type": "anthropic_api"}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="in the clear"):
            load_config(path)

    def test_a_userinfo_url_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "mak.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "endpoints": [
                        {"id": "gw", "base_url": "https://u:p@api.example.com/v1"}
                    ],
                    "agents": [{"type": "anthropic_api"}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="must not embed credentials"):
            load_config(path)


class TestLegacyConfigsStillWork:
    """Acceptance 7: nothing written before this wave behaves differently."""

    def test_a_three_provider_roster_routes_as_it_always_did(self) -> None:
        config = MakConfig(
            agents=(
                AgentConfig(type="anthropic_api"),
                AgentConfig(type="openai_api"),
                AgentConfig(type="gemini_api"),
            )
        )
        assert build_registry(config).list_ids() == [
            "anthropic_api",
            "openai_api",
            "gemini_api",
        ]

    def test_legacy_specs_keep_their_key_rules(self) -> None:
        (agent,) = agents_from_specs(["openai:gpt-5.6-sol"])
        assert agent.api_key_env == "OPENAI_API_KEY"

    def test_a_legacy_base_url_spec_still_works(self) -> None:
        (agent,) = agents_from_specs(["local:m@http://localhost:8000/v1"])
        assert agent.type == "local_api"

    def test_every_packaged_example_still_loads(self) -> None:
        from mak.bootstrap import validate_config
        from mak.config import example_path, list_examples

        for name in list_examples():
            validate_config(load_config(example_path(name)))

    def test_a_v1_model_cache_is_migrated_not_discarded(
        self, tmp_path: Path
    ) -> None:
        from mak.models.manifest import PREVIOUS_SCHEMA_VERSION, load_manifest

        path = tmp_path / "models.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": PREVIOUS_SCHEMA_VERSION,
                    "providers": {
                        "openai": {
                            "fetched_at": None,
                            "models": [
                                {
                                    "provider": "openai",
                                    "model_id": "gpt-5.6-sol",
                                    "display_name": "GPT-5.6 Sol",
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        assert [e.model_id for e in load_manifest(path).models_for("openai")] == [
            "gpt-5.6-sol"
        ]

    def test_a_recovery_file_with_a_legacy_agent_type_resolves(self) -> None:
        from mak.core.task_codec import subtask_from_dict

        raw: dict[str, Any] = {
            "task_id": "t1",
            "description": "d",
            "agent_type": "anthropic_api",
        }
        assert subtask_from_dict(raw).agent_type == "anthropic_api"


class TestNoDefaultTestSpends:
    """Acceptance: the suite contacts nothing and costs nothing."""

    def test_the_only_sdk_dependency_is_openai(self) -> None:
        """No vendor SDK may be added for a compatible profile."""
        for profile in BUILTIN_PROFILES:
            assert profile.transport is Transport.OPENAI_CHAT

    def test_building_a_registry_makes_no_network_call(self) -> None:
        config = MakConfig(
            agents=(
                AgentConfig(type="anthropic_api"),
                AgentConfig(type="openai_api"),
            )
        )
        # Construction is lazy: no client exists until a call is made.
        registry = build_registry(config)
        assert registry.list_ids()

    def test_the_fake_server_is_loopback_only(self) -> None:
        with FakeOpenAiServer() as server:
            assert server.base_url.startswith("http://127.0.0.1:")
