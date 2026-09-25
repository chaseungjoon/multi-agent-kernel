"""``PlannerRoute``: one planner route, complete by construction (D25.3)."""
from __future__ import annotations

import itertools

import pytest
from cli.core.state import CliState

from mak.application import PlannerRoute
from mak.config import PlannerConfig
from mak.core.exceptions import ConfigError

_URL = "http://localhost:11434"


class TestConstruction:
    def test_each_kind_builds(self) -> None:
        assert PlannerRoute.hosted("anthropic", "claude-opus-5").kind == "hosted"
        assert PlannerRoute.endpoint("nvidia", "meta/llama").kind == "endpoint"
        assert PlannerRoute.local("ollama", "qwen", _URL).kind == "local"

    def test_google_is_gemini(self) -> None:
        assert PlannerRoute.hosted("google", "gemini-3").provider == "gemini"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"kind": "hosted", "model": "m", "provider": "nope"},
            {"kind": "hosted", "model": "m", "provider": "anthropic", "base_url": _URL},
            {"kind": "hosted", "model": "m", "provider": "openai", "endpoint_id": "x"},
            {"kind": "endpoint", "model": "m"},
            {"kind": "endpoint", "model": "m", "endpoint_id": "x", "base_url": _URL},
            {"kind": "local", "model": "m", "backend": "ollama"},
            {"kind": "local", "model": "m", "backend": "gemini", "base_url": _URL},
            {"kind": "hosted", "model": "", "provider": "anthropic"},
            {"kind": "carrier-pigeon", "model": "m"},
        ],
    )
    def test_an_incomplete_or_mixed_route_is_refused(
        self, kwargs: dict[str, str]
    ) -> None:
        with pytest.raises(ConfigError):
            PlannerRoute(**kwargs)  # type: ignore[arg-type]


class TestSpec:
    @pytest.mark.parametrize(
        "route",
        [
            PlannerRoute.hosted("anthropic", "claude-opus-5"),
            PlannerRoute.hosted("openai", "gpt-5", "https://gw.example/v1"),
            PlannerRoute.endpoint("nvidia", "meta/llama"),
            PlannerRoute.local("ollama", "qwen2.5-coder:14b", _URL),
            PlannerRoute.local("openai", "my-model", "http://localhost:8000/v1"),
        ],
    )
    def test_spec_round_trips(self, route: PlannerRoute) -> None:
        parsed = PlannerRoute.from_spec(route.spec(), endpoint_ids={"nvidia"})
        assert parsed == route

    def test_a_bare_id_names_no_model(self) -> None:
        with pytest.raises(ConfigError, match="names no model"):
            PlannerRoute.from_spec("claude-opus-5", endpoint_ids=())

    def test_an_endpoint_with_a_url_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="already has an address"):
            PlannerRoute.from_spec("nvidia:m@http://x", endpoint_ids={"nvidia"})

    def test_a_local_spec_reads_its_default_url_from_the_given_env(self) -> None:
        route = PlannerRoute.from_spec(
            "local:m", endpoint_ids=(), env={"MAK_LOCAL_BASE_URL": "http://h:9/v1"}
        )
        assert route == PlannerRoute.local("openai", "m", "http://h:9/v1")


class TestApply:
    def test_hosted_names_its_key_variable(self) -> None:
        planner = PlannerRoute.hosted("gemini", "g").apply(PlannerConfig())
        assert (planner.backend, planner.api_key_env) == ("gemini", "GEMINI_API_KEY")

    def test_a_gateway_names_no_key(self) -> None:
        planner = PlannerRoute.hosted("openai", "m", "https://gw/v1").apply(
            PlannerConfig()
        )
        assert planner.base_url == "https://gw/v1"
        assert planner.api_key_env is None

    def test_every_route_field_is_rewritten(self) -> None:
        stale = PlannerConfig(
            model="x", backend="ollama", base_url=_URL, api_key_env="T", max_retries=7
        )
        planner = PlannerRoute.endpoint("nvidia", "m").apply(stale)
        assert (planner.endpoint, planner.backend, planner.base_url) == (
            "nvidia", None, None,
        )
        assert planner.api_key_env is None
        assert planner.max_retries == 7  # non-route settings are kept

    def test_planner_from_spec_is_the_same_implementation(self) -> None:
        from mak.bootstrap import planner_from_spec

        for spec in ("anthropic:claude-opus-5", f"ollama:qwen@{_URL}"):
            assert planner_from_spec(spec, PlannerConfig(), endpoint_ids=()) == (
                PlannerRoute.from_spec(spec, endpoint_ids=()).apply(PlannerConfig())
            )


# Every way the app changes the planner. Each must leave exactly one route.
_SETTERS = {
    "hosted": lambda s: s.set_cloud_planner("openai", "gpt-5"),
    "endpoint": lambda s: s.set_endpoint_planner("nvidia", "meta/llama"),
    "local": lambda s: s.set_local_planner("ollama", "qwen", _URL),
    "local_off": lambda s: _go_cloud(s),
}


def _go_cloud(state: CliState) -> None:
    from cli.local import go_cloud

    go_cloud(state)


class TestRouteInvariant:
    @pytest.mark.parametrize(
        "sequence", list(itertools.permutations(_SETTERS, 3))
    )
    def test_any_setter_sequence_leaves_exactly_one_route(
        self, sequence: tuple[str, ...]
    ) -> None:
        state = CliState(api_keys={"ANTHROPIC_API_KEY": "sk"})
        for name in sequence:
            _SETTERS[name](state)
            route = state.planner
            facets = {
                "hosted": bool(route.provider),
                "endpoint": bool(route.endpoint_id),
                "local": route.kind == "local" and bool(route.backend),
            }
            assert [k for k, on in facets.items() if on] == [route.kind]

    def test_the_old_field_names_are_read_only(self) -> None:
        state = CliState()
        with pytest.raises(AttributeError):
            state.planner_backend = "ollama"  # type: ignore[misc]
