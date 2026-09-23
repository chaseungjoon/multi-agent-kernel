"""Wave 22.11/22.12: endpoint-aware model and planner selection, and modes."""

from __future__ import annotations

import pytest
from cli.commands import handle_command
from cli.core.state import MODE_CLOUD, MODE_LOCAL, CliState
from rich.console import Console

from mak.endpoints.store import save_user_endpoints
from mak.endpoints.types import EndpointConfig, Location, Transport


def _endpoint(
    endpoint_id: str = "nvidia-work",
    *,
    location: Location = Location.HOSTED,
    key_env: str | None = "NV_KEY",
) -> EndpointConfig:
    return EndpointConfig(
        id=endpoint_id,
        transport=Transport.OPENAI_CHAT,
        base_url="https://nv.example/v1",
        api_key_env=key_env,
        location=location,
        display_name=endpoint_id,
    )


def _run(line: str, state: CliState) -> str:
    console = Console(width=200, no_color=True, highlight=False, record=True)
    handle_command(line, state, console)
    return console.export_text()


@pytest.fixture
def state() -> CliState:
    return CliState()


class TestModelSelection:
    def test_an_endpoint_spec_is_accepted(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        out = _run("/models nvidia-work:meta/llama-3.3-70b-instruct", state)
        assert state.selected_models == [
            "nvidia-work:meta/llama-3.3-70b-instruct"
        ]
        assert "Models:" in out

    def test_the_model_id_keeps_every_colon_after_the_first(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ollama tags and vendor slugs both depend on this rule."""
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        _run("/models nvidia-work:qwen2.5-coder:14b", state)
        assert state.selected_models == ["nvidia-work:qwen2.5-coder:14b"]

    def test_a_missing_key_is_refused_naming_the_variable(
        self, state: CliState
    ) -> None:
        save_user_endpoints((_endpoint(),))
        out = _run("/models nvidia-work:m", state)
        assert "NV_KEY" in out
        assert state.selected_models == []

    def test_a_keyless_endpoint_needs_no_key(self, state: CliState) -> None:
        save_user_endpoints((_endpoint("vllm", key_env=None),))
        _run("/models vllm:local-model", state)
        assert state.selected_models == ["vllm:local-model"]

    def test_a_spec_with_no_model_says_how_to_list_them(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        out = _run("/models nvidia-work", state)
        assert "/endpoint models nvidia-work" in out

    def test_an_unknown_prefix_lists_what_is_known(self, state: CliState) -> None:
        save_user_endpoints((_endpoint(),))
        out = _run("/models mistral:big", state)
        assert "Unknown endpoint or provider" in out
        assert "nvidia-work" in out
        assert "/endpoint add" in out

    def test_the_endpoint_is_remembered_for_completions(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        _run("/models nvidia-work:m", state)
        assert state.endpoint_ids == ["nvidia-work"]

    def test_several_models_on_one_endpoint_coexist(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uniqueness is by agent id now, not by provider."""
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        state.max_agents = 3
        _run("/models nvidia-work:model-a nvidia-work:model-b", state)
        assert len(state.selected_models) == 2

    def test_the_model_browser_shows_a_configured_endpoint(
        self, state: CliState
    ) -> None:
        save_user_endpoints((_endpoint("openrouter", key_env=None),))
        state.endpoint_ids = ["openrouter"]
        state.selected_models = ["openrouter:vendor/model"]

        out = _run("/models", state)

        assert "openrouter" in out
        assert "openrouter:vendor/model" in out

    def test_model_completion_includes_a_configured_endpoint(
        self, state: CliState
    ) -> None:
        from cli.completer import MakCompleter
        from prompt_toolkit.document import Document

        state.endpoint_ids = ["openrouter"]
        state.selected_models = ["openrouter:vendor/model"]
        completions = MakCompleter(state).get_completions(
            Document("/models open", 12),
            None,  # type: ignore[arg-type]
        )

        assert "openrouter:vendor/model" in [item.text for item in completions]


class TestPlannerSelection:
    def test_an_endpoint_spec_sets_route_and_model(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        _run("/planner nvidia-work:meta/llama", state)
        assert state.planner_model == "meta/llama"
        assert state.planner_endpoint_id == "nvidia-work"

    def test_the_legacy_route_fields_are_cleared(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two answers to 'where does this go' is how a planner leaks."""
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        state.planner_backend = "ollama"
        state.planner_base_url = "http://localhost:11434/v1"
        _run("/planner nvidia-work:meta/llama", state)
        assert state.planner_backend == ""
        assert state.planner_base_url == ""

    def test_an_unevaluated_model_says_so(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        out = _run("/planner nvidia-work:meta/llama", state)
        assert "not evaluated" in out

    def test_a_missing_key_is_refused(self, state: CliState) -> None:
        save_user_endpoints((_endpoint(),))
        out = _run("/planner nvidia-work:m", state)
        assert "NV_KEY" in out
        assert state.planner_endpoint_id == ""


class TestPlannerProviderSpec:
    """``/planner`` takes ``provider:model``, the same grammar as ``/models``."""

    def test_a_cloud_spec_records_the_provider(self, state: CliState) -> None:
        state.api_keys["ANTHROPIC_API_KEY"] = "sk-ant"
        out = _run("/planner anthropic:claude-opus-5", state)
        assert state.planner_model == "claude-opus-5"
        assert state.planner_backend == "anthropic"
        assert state.planner_spec() == "anthropic:claude-opus-5"
        assert "Planner: anthropic:claude-opus-5" in out

    def test_a_bare_model_is_refused_naming_the_spec(self, state: CliState) -> None:
        state.api_keys["ANTHROPIC_API_KEY"] = "sk-ant"
        state.planner_model = "claude-sonnet-5"
        out = _run("/planner claude-opus-5", state)
        assert state.planner_model == "claude-sonnet-5"
        assert "anthropic:claude-opus-5" in out

    def test_a_model_from_another_provider_is_refused(
        self, state: CliState
    ) -> None:
        state.api_keys["OPENAI_API_KEY"] = "sk-oai"
        out = _run("/planner openai:claude-opus-5", state)
        assert state.planner_backend == ""
        assert "Unknown model: openai:claude-opus-5" in out

    def test_the_same_model_on_two_providers_routes_where_named(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        state.api_keys["ANTHROPIC_API_KEY"] = "sk-ant"
        _run("/planner nvidia-work:claude-opus-5", state)
        assert state.planner_spec() == "nvidia-work:claude-opus-5"
        _run("/planner anthropic:claude-opus-5", state)
        # The endpoint route must not survive beside the provider just chosen.
        assert state.planner_endpoint_id == ""
        assert state.planner_spec() == "anthropic:claude-opus-5"

    def test_the_status_line_shows_the_spec(self, state: CliState) -> None:
        state.api_keys["ANTHROPIC_API_KEY"] = "sk-ant"
        _run("/planner anthropic:claude-opus-5", state)
        assert "anthropic:claude-opus-5" in _run("/status", state)


class TestPlannerKeyResolution:
    def test_the_endpoint_credential_is_authoritative(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the model-name prefix — that would cross-send credentials."""
        from cli.runner import _resolve_planner_api_key

        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nvidia")
        state.planner_endpoint_id = "nvidia-work"
        # A model whose name *looks* like OpenAI's, served by NVIDIA.
        state.planner_model = "gpt-oss-120b"
        state.api_keys = {"OPENAI_API_KEY": "sk-openai"}
        assert _resolve_planner_api_key(state) == "sk-nvidia"

    def test_a_keyless_endpoint_resolves_to_none(self, state: CliState) -> None:
        from cli.runner import _resolve_planner_api_key

        save_user_endpoints((_endpoint("vllm", key_env=None),))
        state.planner_endpoint_id = "vllm"
        state.api_keys = {"OPENAI_API_KEY": "sk-openai"}
        assert _resolve_planner_api_key(state) is None

    def test_a_recorded_provider_beats_the_model_prefix(
        self, state: CliState
    ) -> None:
        from cli.runner import _resolve_planner_api_key

        state.api_keys = {"ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "sk-oai"}
        state.set_cloud_planner("openai", "claude-lookalike")
        assert _resolve_planner_api_key(state) == "sk-oai"

    def test_without_an_endpoint_the_prefix_still_works(
        self, state: CliState
    ) -> None:
        from cli.runner import _resolve_planner_api_key

        state.planner_model = "claude-opus-5"
        state.api_keys = {"ANTHROPIC_API_KEY": "sk-ant"}
        assert _resolve_planner_api_key(state) == "sk-ant"


class TestModeSemantics:
    def test_a_hosted_endpoint_does_not_flip_the_session_to_local(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug: every hosted compatible service has a base_url.

        Flipping to local mode for one would launch the local-runtime wizard at
        a user who has no local runtime at all.
        """
        save_user_endpoints((_endpoint(),))
        monkeypatch.setenv("NV_KEY", "sk-nv")
        state.mode = MODE_CLOUD
        _run("/models nvidia-work:m", state)
        assert state.mode == MODE_CLOUD

    def test_a_local_endpoint_does_flip_the_session(
        self, state: CliState
    ) -> None:
        save_user_endpoints(
            (_endpoint("vllm", location=Location.LOCAL, key_env=None),)
        )
        state.mode = MODE_CLOUD
        _run("/models vllm:m", state)
        assert state.mode == MODE_LOCAL

    def test_agents_are_local_reads_the_endpoint_location(
        self, state: CliState
    ) -> None:
        from cli.commands import _agents_are_local

        save_user_endpoints(
            (
                _endpoint("hosted-gw"),
                _endpoint("local-gw", location=Location.LOCAL, key_env=None),
            )
        )
        state.selected_models = ["hosted-gw:a"]
        assert _agents_are_local(state) is False
        state.selected_models = ["local-gw:a"]
        assert _agents_are_local(state) is True
        state.selected_models = ["hosted-gw:a", "local-gw:b"]
        assert _agents_are_local(state) is None

    def test_a_private_endpoint_counts_as_not_hosted(
        self, state: CliState
    ) -> None:
        from cli.commands import _agents_are_local

        save_user_endpoints(
            (_endpoint("lan-gw", location=Location.PRIVATE, key_env=None),)
        )
        state.selected_models = ["lan-gw:a"]
        assert _agents_are_local(state) is True

    def test_the_planner_location_reads_the_endpoint(
        self, state: CliState
    ) -> None:
        from cli.commands import _planner_is_local

        save_user_endpoints(
            (
                _endpoint("hosted-gw"),
                _endpoint("local-gw", location=Location.LOCAL, key_env=None),
            )
        )
        state.planner_endpoint_id = "hosted-gw"
        assert _planner_is_local(state) is False
        state.planner_endpoint_id = "local-gw"
        assert _planner_is_local(state) is True


class TestStatus:
    def test_status_shows_the_planner_endpoint(self, state: CliState) -> None:
        state.planner_endpoint_id = "nvidia-work"
        state.planner_model = "meta/llama"
        out = _run("/status", state)
        assert "nvidia-work:meta/llama" in out

    def test_status_labels_each_endpoint_by_location(
        self, state: CliState
    ) -> None:
        save_user_endpoints(
            (
                _endpoint("hosted-gw"),
                _endpoint("lan-gw", location=Location.PRIVATE, key_env=None),
            )
        )
        state.endpoint_ids = ["hosted-gw", "lan-gw"]
        out = _run("/status", state)
        assert "hosted-gw (hosted)" in out
        assert "lan-gw (private)" in out

    def test_status_never_prints_a_key(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NV_KEY", "sk-sentinel")
        save_user_endpoints((_endpoint(),))
        state.endpoint_ids = ["nvidia-work"]
        assert "sk-sentinel" not in _run("/status", state)


class TestLocalPlannerWarning:
    def _config(self, *, agents_local: bool, planner_hosted: bool) -> object:
        from mak.config import AgentConfig, MakConfig, PlannerConfig

        agent = (
            AgentConfig(type="local_api", base_url="http://localhost:8000/v1")
            if agents_local
            else AgentConfig(type="openai_api")
        )
        planner = PlannerConfig(
            model="claude-opus-5",
            backend=None if planner_hosted else "ollama",
            base_url=None if planner_hosted else "http://localhost:11434",
        )
        return MakConfig(agents=(agent,), planner=planner)

    def test_it_fires_for_local_agents_and_a_hosted_planner(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mak.__main__ import warn_local_planner_mismatch

        warn_local_planner_mismatch(
            self._config(agents_local=True, planner_hosted=True)  # type: ignore[arg-type]
        )
        assert "every agent is local" in capsys.readouterr().err

    def test_it_stays_quiet_when_the_planner_is_local_too(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mak.__main__ import warn_local_planner_mismatch

        warn_local_planner_mismatch(
            self._config(agents_local=True, planner_hosted=False)  # type: ignore[arg-type]
        )
        assert capsys.readouterr().err == ""

    def test_it_stays_quiet_for_hosted_agents(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mak.__main__ import warn_local_planner_mismatch

        warn_local_planner_mismatch(
            self._config(agents_local=False, planner_hosted=True)  # type: ignore[arg-type]
        )
        assert capsys.readouterr().err == ""

    def test_a_hosted_compatible_endpoint_does_not_count_as_local(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The old check keyed off a fixed set of 'local' agent types.

        A roster of hosted compatible endpoints would have been treated as
        not-local by accident rather than by understanding.
        """
        from mak.__main__ import warn_local_planner_mismatch
        from mak.config import AgentConfig, MakConfig, PlannerConfig

        config = MakConfig(
            endpoints=(_endpoint("nv"),),
            agents=(AgentConfig(type="", endpoint="nv", model="m"),),
            planner=PlannerConfig(model="claude-opus-5"),
        )
        warn_local_planner_mismatch(config)
        assert capsys.readouterr().err == ""
