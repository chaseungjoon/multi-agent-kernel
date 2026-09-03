"""Every packaged example must load and validate.

A documented example that no longer parses is worse than no example: it is a
config a user copies, trusts, and cannot run. These tests are what stops one
rotting past a schema change.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest
import yaml
from cli.__main__ import _examples

from mak.bootstrap import LOCAL_AGENT_TYPES, build_registry, validate_config
from mak.config import example_path, list_examples, load_config

_LOCAL_EXAMPLES = (
    "local-ollama",
    "local-openai-compatible",
    "fully-local-offline",
)


class TestPackagedExamples:
    def test_the_expected_examples_ship(self) -> None:
        assert set(list_examples()) == {
            *_LOCAL_EXAMPLES,
            "hybrid-cloud-planner-local-agents",
        }

    @pytest.mark.parametrize("name", list_examples())
    def test_every_example_loads_and_validates(self, name: str) -> None:
        config = load_config(example_path(name))
        validate_config(config)

    @pytest.mark.parametrize("name", list_examples())
    def test_every_example_builds_a_registry(self, name: str) -> None:
        # Building a registry constructs no client and makes no network call, so
        # this is a pure "the wiring is real" check.
        config = load_config(example_path(name))
        registry = build_registry(config)
        for agent in config.agents:
            assert registry.get(agent.type) is not None

    @pytest.mark.parametrize("name", _LOCAL_EXAMPLES)
    def test_the_local_examples_declare_a_local_agent_with_an_endpoint(
        self, name: str
    ) -> None:
        config = load_config(example_path(name))
        for agent in config.agents:
            assert agent.type in LOCAL_AGENT_TYPES
            assert agent.base_url is not None
            assert agent.base_url.startswith("http")

    def test_the_hybrid_example_keeps_a_hosted_planner_and_local_agents(self) -> None:
        config = load_config(example_path("hybrid-cloud-planner-local-agents"))
        assert config.planner.model.startswith("claude")
        assert config.planner.backend is None
        assert config.planner.base_url is None
        assert all(a.type in LOCAL_AGENT_TYPES for a in config.agents)

    def test_the_offline_example_contacts_nothing(self) -> None:
        config = load_config(example_path("fully-local-offline"))
        assert config.models.auto_refresh is False
        assert config.git.auto_push is False
        assert config.planner.backend == "ollama"

    @pytest.mark.parametrize("name", _LOCAL_EXAMPLES)
    def test_local_examples_allow_minutes_per_call(self, name: str) -> None:
        # Local generation is measured in minutes and the per-agent timeout is
        # what bounds the call; the 300s default would cut real work off.
        config = load_config(example_path(name))
        assert all(agent.timeout >= 900 for agent in config.agents)


class TestExamplesCommand:
    def test_listing_exits_zero_and_names_every_example(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            assert _examples([]) == 0
        for name in list_examples():
            assert name in out.getvalue()

    @pytest.mark.parametrize("name", list_examples())
    def test_printing_one_yields_parseable_yaml(self, name: str) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            assert _examples([name]) == 0
        data = yaml.safe_load(out.getvalue())
        assert isinstance(data, dict)
        assert "agents" in data

    def test_an_unknown_name_exits_one_listing_what_exists(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _examples(["nope"]) == 1
        assert "local-ollama" in capsys.readouterr().err

    def test_a_traversal_argument_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The name reaches here from the command line; joining it to a package
        # path unchecked is how "print my config" becomes a file read.
        assert _examples(["../config"]) == 1
        assert "no packaged example" in capsys.readouterr().err


class TestPackagingExtras:
    """Wave 15.17 (D8): every SDK is an extra, and every message names it."""

    def _pyproject(self) -> dict[str, object]:
        import tomllib
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as handle:
            return tomllib.load(handle)

    def test_the_five_extras_are_declared(self) -> None:
        extras = self._pyproject()["project"]["optional-dependencies"]  # type: ignore[index]
        assert {"anthropic", "openai", "gemini", "local", "all"} <= set(extras)

    def test_the_local_extra_is_empty(self) -> None:
        # The strongest form of this wave's promise: a fully-local install needs
        # no provider SDK at all.
        extras = self._pyproject()["project"]["optional-dependencies"]  # type: ignore[index]
        assert extras["local"] == []

    def test_the_sdks_stay_in_base_dependencies_this_wave(self) -> None:
        # D8: removing them would strip the SDKs out from under every existing
        # user on their next `mak update`. That change ships with Wave 9.3.
        deps = " ".join(self._pyproject()["project"]["dependencies"])  # type: ignore[index,arg-type]
        assert "anthropic" in deps
        assert "openai" in deps
        assert "google-genai" in deps

    def test_examples_are_package_data(self) -> None:
        # Otherwise `mak examples` finds nothing in a pipx / uv tool install.
        data = self._pyproject()["tool"]["setuptools"]["package-data"]["mak"]  # type: ignore[index]
        assert "examples/*.yaml" in data


class TestLazyImportMessagesNameTheirExtra:
    @pytest.mark.parametrize(
        "module,attr,extra",
        [
            (
                "mak.agent_runner.adapters.anthropic_api_adapter",
                "anthropic",
                "anthropic",
            ),
            ("mak.agent_runner.adapters.openai_api_adapter", "openai", "openai"),
            ("mak.agent_runner.adapters.gemini_api_adapter", "google", "gemini"),
        ],
    )
    def test_an_adapter_names_its_extra(
        self, module: str, attr: str, extra: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins
        import importlib

        real_import = builtins.__import__

        def blocked(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".")[0] == attr:
                raise ImportError(f"No module named {attr!r}")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", blocked)
        adapter_module = importlib.import_module(module)
        adapter_cls = next(
            value
            for name, value in vars(adapter_module).items()
            if name.endswith("ApiAdapter") and isinstance(value, type)
        )
        adapter = adapter_cls()
        with pytest.raises(Exception) as excinfo:
            adapter._get_client()
        assert f"multi-agent-kernel[{extra}]" in str(excinfo.value)

    @pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
    def test_a_model_list_source_names_its_extra(
        self, provider: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        from mak.models.providers import (
            AnthropicSource,
            GeminiSource,
            ModelFetchError,
            OpenAiSource,
        )

        sources = {
            "anthropic": (AnthropicSource, "anthropic"),
            "openai": (OpenAiSource, "openai"),
            "gemini": (GeminiSource, "google"),
        }
        source_cls, top_level = sources[provider]
        real_import = builtins.__import__

        def blocked(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".")[0] == top_level:
                raise ImportError(f"No module named {top_level!r}")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", blocked)
        with pytest.raises(ModelFetchError) as excinfo:
            source_cls().fetch("k")
        assert f"multi-agent-kernel[{provider}]" in str(excinfo.value)

    @pytest.mark.parametrize(
        "model,extra",
        [
            ("claude-opus-5", "anthropic"),
            ("gpt-5.6-sol", "openai"),
            ("gemini-3.5-flash", "gemini"),
        ],
    )
    def test_a_planner_backend_names_its_extra(
        self, model: str, extra: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        from mak.core.exceptions import PlannerFailedError
        from mak.planner.llm import build_planner_llm

        top_level = {"anthropic": "anthropic", "openai": "openai", "gemini": "google"}[
            extra
        ]
        real_import = builtins.__import__

        def blocked(name: str, *args: object, **kwargs: object) -> object:
            if name.split(".")[0] == top_level:
                raise ImportError(f"No module named {top_level!r}")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", blocked)
        llm = build_planner_llm(model)
        with pytest.raises(PlannerFailedError) as excinfo:
            llm._get_client()  # type: ignore[attr-defined]
        assert f"multi-agent-kernel[{extra}]" in str(excinfo.value)
