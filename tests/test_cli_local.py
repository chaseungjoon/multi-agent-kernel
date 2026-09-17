"""Wave 15, Phase C: mode, the /local wizard, and the local-aware commands.

Every test drives a fake runtime and a fake client, so nothing here opens a
socket or spawns a model.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import cli.local as local_mod
import pytest
from cli.commands import handle_command
from cli.completer import COMMANDS
from cli.core.state import MODE_CLOUD, MODE_HYBRID, MODE_LOCAL, CliState
from cli.local import cmd_local, run_wizard, spec_for
from cli.runner import _apply_state_to_config, _resolve_planner_api_key
from cli.ui import print_status
from rich.console import Console

from mak.local import LocalRuntime, OllamaError, OllamaModel, PullProgress
from mak.local.runtime import KIND_OLLAMA, KIND_OPENAI_COMPATIBLE

_URL = "http://localhost:11434"
_MODEL = "qwen2.5-coder:14b"


class FakeClient:
    """An OllamaClient stand-in for the TUI."""

    def __init__(
        self,
        *,
        models: tuple[str, ...] = (_MODEL,),
        loaded: tuple[str, ...] = (),
        down: bool = False,
        context_length: int | None = 32768,
        pull_error: str | None = None,
    ) -> None:
        self._models = models
        self._loaded = loaded
        self._down = down
        self._context_length = context_length
        self._pull_error = pull_error
        self.pulled: list[str] = []

    def _guard(self) -> None:
        if self._down:
            raise OllamaError(f"cannot reach {_URL}/api/version: Connection refused")

    def version(self) -> str:
        self._guard()
        return "0.5.7"

    def list_models(self) -> list[OllamaModel]:
        self._guard()
        return [
            OllamaModel(name=name, parameter_size="14.8B", quantization="Q4_K_M")
            for name in self._models
        ]

    def running(self) -> list[str]:
        self._guard()
        return list(self._loaded)

    def show(self, model: str) -> OllamaModel:
        self._guard()
        return OllamaModel(name=model, context_length=self._context_length)

    def pull(self, model: str) -> Iterator[PullProgress]:
        self._guard()
        if self._pull_error:
            raise OllamaError(self._pull_error)
        self.pulled.append(model)
        yield PullProgress(status="pulling", total=100, completed=50)
        yield PullProgress(status="success")


def _ollama(models: tuple[str, ...] = (_MODEL,)) -> LocalRuntime:
    return LocalRuntime(
        kind=KIND_OLLAMA,
        name="Ollama",
        base_url=_URL,
        version="0.5.7",
        models=models,
    )


@pytest.fixture(autouse=True)
def _seams() -> Iterator[None]:
    yield
    local_mod.reset_seams()


def _install(
    client: FakeClient, runtimes: list[LocalRuntime] | None = None
) -> FakeClient:
    local_mod.set_seams(
        discover_fn=lambda: list(runtimes or []),
        client_factory=lambda _url: client,  # type: ignore[arg-type,return-value]
    )
    return client


def _console() -> Console:
    return Console(file=io.StringIO(), highlight=False, width=100)


def _output(console: Console) -> str:
    stream = console.file
    assert isinstance(stream, io.StringIO)
    return stream.getvalue()


def _answers(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> list[str]:
    """Drive the wizard through its single input seam."""
    asked: list[str] = []
    queue = list(replies)

    def fake_ask(_console: Console, prompt: str, default: str = "") -> str:
        asked.append(prompt)
        return queue.pop(0) if queue else default

    monkeypatch.setattr(local_mod, "_ask", fake_ask)
    return asked


def _local_state(**kwargs: Any) -> CliState:
    state = CliState(
        mode=MODE_LOCAL,
        local_kind=KIND_OLLAMA,
        local_base_url=_URL,
        local_models=[_MODEL],
        **kwargs,
    )
    return state


# ── the wizard ────────────────────────────────────────────────────────────────


class TestWizard:
    def test_happy_path_sets_every_state_field(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _install(FakeClient(), [_ollama()])
        # model 1 · planner "same local model" · do not save
        _answers(monkeypatch, ["1", "1", "n"])
        state = CliState()
        console = _console()

        assert run_wizard(state, console) is True
        assert state.mode == MODE_LOCAL
        assert state.local_kind == KIND_OLLAMA
        assert state.local_base_url == _URL
        assert state.selected_models == [f"ollama:{_MODEL}@{_URL}"]
        assert state.planner_model == _MODEL
        assert state.planner_backend == "ollama"
        assert state.planner_base_url == _URL
        # D13: nothing written without an explicit yes.
        assert not (tmp_path / "mak.yaml").exists()

    def test_saving_writes_a_config_that_loads_and_validates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _install(FakeClient(), [_ollama()])
        _answers(monkeypatch, ["1", "1", "y"])
        state = CliState()

        run_wizard(state, _console())
        written = tmp_path / "mak.yaml"
        assert written.exists()

        from mak.bootstrap import validate_config
        from mak.config import load_config

        config = load_config(written)
        validate_config(config)
        assert config.agents[0].type == "ollama_api"
        assert config.agents[0].base_url == _URL
        assert config.planner.backend == "ollama"

    def test_nothing_detected_prints_guidance_and_changes_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(FakeClient(), [])
        state = CliState()
        console = _console()

        assert run_wizard(state, console) is False
        text = _output(console)
        assert "brew install ollama" in text
        assert "ollama serve" in text
        assert "/local url" in text
        assert state.mode == MODE_CLOUD
        assert state.local_base_url == ""

    def test_with_no_model_installed_it_offers_and_pulls_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        client = _install(FakeClient(models=()), [_ollama(models=())])
        # pull suggestion 1 · planner "same" · no save
        _answers(monkeypatch, ["1", "1", "n"])
        state = CliState()

        assert run_wizard(state, _console()) is True
        assert client.pulled == ["qwen2.5-coder:7b"]
        assert state.selected_models == [f"ollama:qwen2.5-coder:7b@{_URL}"]

    def test_a_cloud_planner_choice_yields_hybrid(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _install(FakeClient(), [_ollama()])
        # model 1 · planner option 3 (cloud) · no save
        _answers(monkeypatch, ["1", "3", "n"])
        state = CliState(api_keys={"ANTHROPIC_API_KEY": "sk-x"})

        run_wizard(state, _console())
        assert state.mode == MODE_HYBRID
        assert state.planner_model == "claude-opus-5"
        assert state.planner_backend == ""
        assert state.planner_base_url == ""

    def test_the_context_window_is_reported_before_the_first_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _install(FakeClient(context_length=8192), [_ollama()])
        _answers(monkeypatch, ["1", "1", "n"])
        console = _console()

        run_wizard(CliState(), console)
        text = _output(console)
        assert "8,192 tokens" in text
        # D11's footgun, made visible before a bad run rather than after one.
        assert "dependency_context_bytes" in text


# ── sub-commands ──────────────────────────────────────────────────────────────


class TestSubCommands:
    def test_status_reports_endpoint_version_and_models(self) -> None:
        _install(FakeClient(loaded=(_MODEL,)))
        console = _console()
        cmd_local(["status"], _local_state(), console)
        text = _output(console)
        assert _URL in text
        assert "0.5.7" in text
        assert _MODEL in text

    def test_models_lists_live(self) -> None:
        _install(FakeClient(models=(_MODEL, "llama3.1:8b")))
        state = _local_state()
        console = _console()
        cmd_local(["models"], state, console)
        assert "llama3.1:8b" in _output(console)
        assert state.local_models == [_MODEL, "llama3.1:8b"]

    def test_use_sets_the_agent_roster(self) -> None:
        _install(FakeClient())
        state = _local_state()
        cmd_local(["use", _MODEL], state, _console())
        assert state.selected_models == [f"ollama:{_MODEL}@{_URL}"]

    def test_use_rejects_a_model_that_is_not_installed(self) -> None:
        _install(FakeClient())
        state = _local_state()
        console = _console()
        cmd_local(["use", "nope:7b"], state, console)
        assert "/local pull nope:7b" in _output(console)
        assert state.selected_models == []

    def test_planner_sets_backend_and_endpoint(self) -> None:
        _install(FakeClient())
        state = _local_state()
        cmd_local(["planner", _MODEL], state, _console())
        assert state.planner_model == _MODEL
        assert state.planner_backend == "ollama"
        assert state.planner_base_url == _URL

    def test_pull_reports_success_and_records_the_model(self) -> None:
        client = _install(FakeClient(models=()))
        state = _local_state()
        state.local_models = []
        console = _console()
        cmd_local(["pull", "qwen2.5-coder:7b"], state, console)
        assert client.pulled == ["qwen2.5-coder:7b"]
        assert "qwen2.5-coder:7b" in state.local_models

    def test_pull_reports_a_failure_as_one_line(self) -> None:
        _install(FakeClient(pull_error="model 'nope' not found"))
        console = _console()
        cmd_local(["pull", "nope"], _local_state(), console)
        assert "not found" in _output(console)

    def test_url_rejects_a_non_http_endpoint(self) -> None:
        _install(FakeClient())
        state = CliState()
        console = _console()
        cmd_local(["url", "ftp://host:1"], state, console)
        assert "http:// or https://" in _output(console)
        assert state.local_base_url == ""

    def test_url_accepts_and_probes_a_good_endpoint(self) -> None:
        _install(FakeClient())
        state = CliState()
        cmd_local(["url", f"{_URL}/"], state, _console())
        assert state.local_base_url == _URL
        assert state.mode == MODE_LOCAL
        assert state.local_models == [_MODEL]

    def test_off_restores_cloud_mode_and_keeps_keys(self) -> None:
        state = _local_state(api_keys={"ANTHROPIC_API_KEY": "sk-x"})
        state.selected_models = [f"ollama:{_MODEL}@{_URL}"]
        state.planner_backend = "ollama"
        cmd_local(["off"], state, _console())
        assert state.mode == MODE_CLOUD
        assert state.local_base_url == ""
        assert state.selected_models == []
        assert state.planner_backend == ""
        assert state.api_keys == {"ANTHROPIC_API_KEY": "sk-x"}

    @pytest.mark.parametrize(
        "args",
        [["status"], ["models"], ["use", _MODEL], ["planner", _MODEL], ["pull", "m"]],
    )
    def test_every_sub_command_survives_a_dead_server(
        self, args: list[str]
    ) -> None:
        # One red line naming the endpoint, never a traceback, never a crash of
        # the prompt loop.
        _install(FakeClient(down=True))
        state = _local_state()
        before = (state.selected_models[:], state.planner_model)
        console = _console()
        cmd_local(args, state, console)
        assert "cannot reach" in _output(console)
        assert (state.selected_models, state.planner_model) == before

    def test_a_sub_command_without_a_runtime_says_so(self) -> None:
        console = _console()
        cmd_local(["status"], CliState(), console)
        assert "No local runtime configured" in _output(console)


# ── /mode and the local-aware commands ────────────────────────────────────────


class TestMode:
    def test_bare_mode_lists_all_three(self) -> None:
        console = _console()
        handle_command("/mode", CliState(), console)
        text = _output(console)
        for mode in (MODE_CLOUD, MODE_LOCAL, MODE_HYBRID):
            assert mode in text

    def test_switching_to_local_requires_a_runtime(self) -> None:
        state = CliState(api_keys={"ANTHROPIC_API_KEY": "sk-x"})
        console = _console()
        handle_command("/mode local", state, console)
        assert "/local" in _output(console)
        assert state.mode == MODE_CLOUD

    def test_switching_to_cloud_requires_a_key(self) -> None:
        state = _local_state()
        console = _console()
        handle_command("/mode cloud", state, console)
        assert "/apikey" in _output(console)
        assert state.mode == MODE_LOCAL

    def test_hybrid_requires_both(self) -> None:
        state = _local_state()
        console = _console()
        handle_command("/mode hybrid", state, console)
        assert "/apikey" in _output(console)

    def test_a_configured_switch_succeeds(self) -> None:
        state = _local_state(api_keys={"ANTHROPIC_API_KEY": "sk-x"})
        handle_command("/mode hybrid", state, _console())
        assert state.mode == MODE_HYBRID

    def test_hybrid_with_cloud_agents_offers_local_agents(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _local_state(
            api_keys={"ANTHROPIC_API_KEY": "sk-x"},
            selected_models=["anthropic:claude-opus-5"],
        )
        state.mode = MODE_CLOUD
        asked = _answers(monkeypatch, ["y", "1"])
        handle_command("/mode hybrid", state, _console())
        assert len(asked) == 2  # confirm + agents; the cloud planner already fits
        assert state.selected_models == [f"ollama:{_MODEL}@{_URL}"]
        assert state.planner_model == "claude-opus-5"
        assert state.mode == MODE_HYBRID

    def test_local_with_cloud_planner_and_agents_offers_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _local_state(
            api_keys={"ANTHROPIC_API_KEY": "sk-x"},
            selected_models=["anthropic:claude-opus-5"],
        )
        state.mode = MODE_CLOUD
        _answers(monkeypatch, ["", "1", "1"])  # Enter = yes
        handle_command("/mode local", state, _console())
        assert state.selected_models == [f"ollama:{_MODEL}@{_URL}"]
        assert state.planner_model == _MODEL
        assert state.planner_base_url == _URL
        assert state.mode == MODE_LOCAL

    def test_cloud_with_local_models_offers_cloud_ones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _local_state(
            api_keys={"ANTHROPIC_API_KEY": "sk-x"},
            selected_models=[f"ollama:{_MODEL}@{_URL}"],
        )
        state.planner_model = _MODEL
        state.planner_backend = "ollama"
        state.planner_base_url = _URL
        _answers(monkeypatch, ["y", "1", "1"])
        handle_command("/mode cloud", state, _console())
        assert state.selected_models[0].startswith("anthropic:")
        assert state.planner_base_url == ""
        assert state.planner_backend == ""
        assert state.mode == MODE_CLOUD

    def test_declining_keeps_the_combination_but_sets_the_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _local_state(
            api_keys={"ANTHROPIC_API_KEY": "sk-x"},
            selected_models=["anthropic:claude-opus-5"],
        )
        state.planner_model = _MODEL
        state.planner_base_url = _URL
        state.mode = MODE_CLOUD
        _answers(monkeypatch, ["n"])
        console = _console()
        handle_command("/mode local", state, console)
        assert state.selected_models == ["anthropic:claude-opus-5"]
        assert state.planner_model == _MODEL
        assert state.mode == MODE_LOCAL
        assert "Keeping the current models" in _output(console)

    def test_skipping_a_pick_keeps_that_part(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _local_state(
            api_keys={"ANTHROPIC_API_KEY": "sk-x"},
            selected_models=["anthropic:claude-opus-5"],
        )
        state.mode = MODE_CLOUD
        _answers(monkeypatch, ["y", "", "1"])  # keep agents, pick a planner
        handle_command("/mode local", state, _console())
        assert state.selected_models == ["anthropic:claude-opus-5"]
        assert state.planner_base_url == _URL

    def test_a_fitting_combination_never_asks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _local_state(
            api_keys={"ANTHROPIC_API_KEY": "sk-x"},
            selected_models=[f"ollama:{_MODEL}@{_URL}"],
        )
        monkeypatch.setattr(
            local_mod, "_ask", lambda *_a, **_k: pytest.fail("must not ask")
        )
        handle_command("/mode hybrid", state, _console())
        assert state.mode == MODE_HYBRID

    def test_an_unknown_mode_is_rejected(self) -> None:
        state = CliState()
        console = _console()
        handle_command("/mode offline", state, console)
        assert "expects one of" in _output(console)


class TestLocalAwareCommands:
    def test_models_accepts_a_local_spec_with_no_key(self) -> None:
        # The _KEY_ENV lookup must not reject a keyless provider.
        state = _local_state()
        console = _console()
        handle_command(f"/models ollama:{_MODEL}", state, console)
        assert state.selected_models == [f"ollama:{_MODEL}@{_URL}"]
        assert "No API key" not in _output(console)

    def test_models_accepts_an_explicit_endpoint_without_a_runtime(self) -> None:
        state = CliState()
        handle_command("/models local:m@http://gpu:8000/v1", state, _console())
        assert state.selected_models == ["local:m@http://gpu:8000/v1"]
        assert state.mode == MODE_LOCAL

    def test_models_without_an_endpoint_or_a_runtime_explains(self) -> None:
        state = CliState()
        console = _console()
        handle_command("/models ollama:m", state, console)
        assert "no local runtime is configured" in _output(console)
        assert state.selected_models == []

    def test_bare_models_lists_the_live_runtime_in_local_mode(self) -> None:
        _install(FakeClient(models=("only-local:1b",)))
        console = _console()
        handle_command("/models", _local_state(), console)
        assert "only-local:1b" in _output(console)

    def test_planner_accepts_a_local_model(self) -> None:
        state = _local_state()
        handle_command(f"/planner ollama:{_MODEL}", state, _console())
        assert state.planner_model == _MODEL
        assert state.planner_backend == "ollama"
        assert state.planner_base_url == _URL

    def test_planner_accepts_an_installed_local_model_by_bare_name(self) -> None:
        state = _local_state()
        handle_command(f"/planner {_MODEL}", state, _console())
        assert state.planner_backend == "ollama"

    def test_refresh_models_also_refreshes_the_local_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cli.commands as commands_mod

        class FakeRegistry:
            def refresh_now(self, _keys: dict[str, str]) -> Any:
                raise RuntimeError("offline")

        monkeypatch.setattr(commands_mod, "registry", FakeRegistry)
        _install(FakeClient(models=(_MODEL, "llama3.1:8b")))
        # Cloud mode: a runtime named with /local url still gets refreshed.
        state = _local_state()
        state.mode = MODE_CLOUD
        console = _console()
        handle_command("/refresh-models", state, console)
        assert state.local_models == [_MODEL, "llama3.1:8b"]
        assert "+ llama3.1:8b" in _output(console)

    def test_refresh_models_keeps_the_cached_list_when_the_runtime_is_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cli.commands as commands_mod

        class FakeRegistry:
            def refresh_now(self, _keys: dict[str, str]) -> Any:
                raise RuntimeError("offline")

        monkeypatch.setattr(commands_mod, "registry", FakeRegistry)
        _install(FakeClient(down=True))
        state = _local_state()
        console = _console()
        handle_command("/refresh-models", state, console)
        assert state.local_models == [_MODEL]
        assert "keeping cached list" in _output(console)

    def test_models_and_planner_list_local_models_in_cloud_mode(self) -> None:
        state = _local_state()
        state.mode = MODE_CLOUD
        for command in ("/models", "/planner"):
            console = _console()
            handle_command(command, state, console)
            text = _output(console)
            assert f"ollama:{_MODEL}" in text
            assert text.index("Local") < text.index("Cloud")

    def test_completer_groups_local_models_before_cloud_in_any_mode(self) -> None:
        from cli.completer import MakCompleter
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        state = _local_state()
        state.mode = MODE_CLOUD
        completer = MakCompleter(state)
        for line in ("/models ", "/planner "):
            rows = completer.get_completions(
                Document(line, len(line)), CompleteEvent()
            )
            texts = [row.text for row in rows]
            displays = [row.display_text for row in rows]
            assert displays[0] == "── Local ──"
            assert texts[1] == f"ollama:{_MODEL}"
            assert "── Cloud ──" in displays
        typed = "/models qwen"
        rows = completer.get_completions(Document(typed, len(typed)), CompleteEvent())
        assert f"ollama:{_MODEL}" in [row.text for row in rows]

    def test_completer_has_no_local_group_without_a_runtime(self) -> None:
        from cli.completer import MakCompleter
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        rows = MakCompleter(CliState()).get_completions(
            Document("/models ", 8), CompleteEvent()
        )
        assert all("Local" not in row.display_text for row in rows)

    def test_the_completer_offers_the_two_new_commands(self) -> None:
        names = {name for name, _ in COMMANDS}
        assert "/local" in names
        assert "/mode" in names


# ── status and runner threading ───────────────────────────────────────────────


class TestStatusAndThreading:
    def test_status_shows_the_mode_in_every_mode(self) -> None:
        for state in (CliState(), _local_state()):
            console = _console()
            print_status(console, state)
            assert state.mode in _output(console)

    def test_status_shows_the_runtime_row_when_local(self) -> None:
        console = _console()
        print_status(console, _local_state())
        text = _output(console)
        assert "runtime" in text
        assert _URL in text

    def test_a_local_roster_threads_into_a_local_config(self) -> None:
        from mak.config import MakConfig

        state = _local_state()
        state.selected_models = [f"ollama:{_MODEL}@{_URL}"]
        config = _apply_state_to_config(MakConfig(), state)
        assert [a.type for a in config.agents] == ["ollama_api"]
        assert config.agents[0].base_url == _URL

    def test_a_hybrid_roster_threads_both(self) -> None:
        from mak.config import MakConfig

        state = CliState(mode=MODE_HYBRID, local_base_url=_URL)
        state.selected_models = ["anthropic:claude-opus-5", f"ollama:{_MODEL}@{_URL}"]
        config = _apply_state_to_config(MakConfig(), state)
        assert [a.type for a in config.agents] == ["anthropic_api", "ollama_api"]

    def test_a_cloud_roster_is_unchanged(self) -> None:
        from mak.config import MakConfig

        state = CliState(selected_models=["anthropic:claude-opus-5"])
        config = _apply_state_to_config(MakConfig(), state)
        assert [a.type for a in config.agents] == ["anthropic_api"]
        assert config.agents[0].base_url is None

    def test_a_mismatched_roster_is_honored_not_refused(self) -> None:
        # /mode offers to fix a mismatch; a user who declined chose it.
        from mak.config import MakConfig

        state = _local_state(selected_models=["anthropic:claude-opus-5"])
        config = _apply_state_to_config(MakConfig(), state)
        assert [a.type for a in config.agents] == ["anthropic_api"]

    def test_a_local_planner_resolves_to_no_api_key(self) -> None:
        state = _local_state(api_keys={"ANTHROPIC_API_KEY": "sk-real"})
        state.planner_backend = "ollama"
        state.planner_base_url = _URL
        assert _resolve_planner_api_key(state) is None

    def test_a_cloud_planner_still_resolves_its_key(self) -> None:
        state = CliState(api_keys={"ANTHROPIC_API_KEY": "sk-real"})
        assert _resolve_planner_api_key(state) == "sk-real"


class TestSpecFor:
    def test_ollama_runtime_yields_an_ollama_spec(self) -> None:
        assert spec_for(_local_state(), _MODEL) == f"ollama:{_MODEL}@{_URL}"

    def test_an_openai_compatible_runtime_yields_a_local_spec(self) -> None:
        state = CliState(
            local_kind=KIND_OPENAI_COMPATIBLE, local_base_url="http://h:8000/v1"
        )
        assert spec_for(state, "m") == "local:m@http://h:8000/v1"


# ── first-run setup (step 13) ─────────────────────────────────────────────────


class TestFirstRunSetup:
    """D12: a machine with no API key must reach the prompt."""

    def _fake_menu(
        self, monkeypatch: pytest.MonkeyPatch, choice: int
    ) -> dict[str, Any]:
        import cli.setup as setup_mod

        calls: dict[str, Any] = {"key_setup": 0, "wizard": 0}
        monkeypatch.setattr(setup_mod, "_ask_choice", lambda _c, _n: choice)

        def fake_key_setup(
            state: CliState,
            _console: Console,
            *,
            editing: bool = False,
            planner_only: bool = False,
        ) -> bool:
            calls["key_setup"] += 1
            calls["planner_only"] = planner_only
            state.api_keys["ANTHROPIC_API_KEY"] = "sk-x"
            return True

        monkeypatch.setattr(setup_mod, "run_key_setup", fake_key_setup)
        return calls

    def test_choosing_local_with_ollama_running_sets_local_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from cli.setup import run_setup

        _install(FakeClient(), [_ollama()])
        calls = self._fake_menu(monkeypatch, 1)  # 0-based: Local
        _answers(monkeypatch, ["1", "1", "n"])
        state = CliState()

        assert run_setup(state, _console()) is True
        assert state.mode == MODE_LOCAL
        assert state.selected_models == [f"ollama:{_MODEL}@{_URL}"]
        assert calls["key_setup"] == 0  # no key was asked for, or needed

    def test_choosing_local_with_nothing_detected_still_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.setup import run_setup

        _install(FakeClient(down=True), [])
        self._fake_menu(monkeypatch, 1)
        console = _console()

        # Guidance, not a failure: the user still reaches the prompt.
        assert run_setup(CliState(), console) is True
        assert "brew install ollama" in _output(console)

    def test_choosing_cloud_runs_the_unchanged_key_wizard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.setup import run_setup

        _install(FakeClient(), [])
        calls = self._fake_menu(monkeypatch, 0)  # Cloud
        state = CliState()

        assert run_setup(state, _console()) is True
        assert calls["key_setup"] == 1
        assert calls["planner_only"] is False
        assert state.mode == MODE_CLOUD

    def test_choosing_hybrid_asks_for_a_planner_key_then_runs_the_wizard(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from cli.setup import run_setup

        _install(FakeClient(), [_ollama()])
        calls = self._fake_menu(monkeypatch, 2)  # Hybrid
        # model 1 · planner option 3 (cloud) · no save
        _answers(monkeypatch, ["1", "3", "n"])
        state = CliState()

        assert run_setup(state, _console()) is True
        assert calls["planner_only"] is True
        assert state.mode == MODE_HYBRID
        assert state.selected_models == [f"ollama:{_MODEL}@{_URL}"]

    def test_apikey_editing_never_asks_about_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.setup import run_setup

        calls = self._fake_menu(monkeypatch, 1)
        monkeypatch.setattr(
            "cli.setup._ask_choice",
            lambda _c, _n: pytest.fail("editing must not show the mode menu"),
        )
        assert run_setup(CliState(), _console(), editing=True) is True
        assert calls["key_setup"] == 1
        assert calls["planner_only"] is False


def test_the_app_no_longer_exits_when_no_key_is_set() -> None:
    """Structural: MakCli.run must not sys.exit merely because no key is set."""
    import ast
    import inspect

    from cli.app import MakCli

    source = inspect.getsource(MakCli.run)
    tree = ast.parse(source.lstrip())
    exits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exit"
    ]
    # Exactly one remains: a *cancelled* setup. Before Wave 15 the same call
    # fired whenever no key was present, which locked a fully-offline user out
    # of the prompt entirely.
    assert len(exits) == 1
    assert "has_local_runtime" in source


# ── remembered hosts ──────────────────────────────────────────────────────────

_REMOTE = "http://100.124.220.35:11434"
_OTHER = "http://gpu-box:11434"


def _remote_runtime(url: str, models: tuple[str, ...]) -> LocalRuntime:
    return LocalRuntime(
        kind=KIND_OLLAMA, name="Ollama", base_url=url, version="0.20.2", models=models
    )


class TestRememberedHosts:
    def test_url_is_remembered_and_restored_next_session(self) -> None:
        from cli.local import restore_saved_hosts

        _install(FakeClient(models=("remote-a:7b",)))
        cmd_local(["url", _REMOTE], CliState(), _console())

        fresh = CliState()
        restore_saved_hosts(fresh)
        assert fresh.local_base_url == _REMOTE
        assert fresh.local_models == ["remote-a:7b"]
        assert fresh.has_local_runtime()

    def test_a_second_url_keeps_both_hosts(self) -> None:
        from cli.local import restore_saved_hosts

        state = CliState()
        _install(FakeClient(models=("remote-a:7b",)))
        cmd_local(["url", _REMOTE], state, _console())
        _install(FakeClient(models=("other-b:14b",)))
        cmd_local(["url", _OTHER], state, _console())

        fresh = CliState()
        restore_saved_hosts(fresh)
        assert fresh.local_base_url == _OTHER
        assert [h.url for h in fresh.all_local_hosts()] == [_OTHER, _REMOTE]

    def test_off_disconnects_but_forget_removes(self) -> None:
        from cli.local import restore_saved_hosts

        state = CliState()
        _install(FakeClient())
        cmd_local(["url", _REMOTE], state, _console())
        cmd_local(["off"], state, _console())

        fresh = CliState()
        restore_saved_hosts(fresh)
        assert fresh.local_base_url == ""
        assert [h.url for h in fresh.local_hosts] == [_REMOTE]

        cmd_local(["forget", _REMOTE], fresh, _console())
        again = CliState()
        restore_saved_hosts(again)
        assert again.local_hosts == []

    def test_a_corrupt_file_is_no_saved_hosts(self) -> None:
        from cli.core.local_hosts import hosts_path
        from cli.local import restore_saved_hosts

        hosts_path().parent.mkdir(parents=True, exist_ok=True)
        hosts_path().write_text("{not json", encoding="utf-8")
        state = CliState()
        restore_saved_hosts(state)
        assert state.local_hosts == []
        assert state.local_base_url == ""

    def test_bare_local_lists_this_machine_then_every_remote_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cli.core.state import LocalHost

        _install(FakeClient(), [_ollama()])
        local_mod.set_seams(
            probe_host_fn=lambda host: (
                _remote_runtime(_REMOTE, ("remote-a:7b",))
                if host.url == _REMOTE
                else None
            )
        )
        state = CliState(
            local_hosts=[
                LocalHost(url=_REMOTE, models=["remote-a:7b"]),
                LocalHost(url=_OTHER, models=["other-b:14b"]),
            ]
        )
        # An overview only: it must never prompt.
        monkeypatch.setattr(
            local_mod, "_ask", lambda *_a, **_k: pytest.fail("/local must not ask")
        )
        console = _console()
        cmd_local([], state, console)
        text = _output(console)
        assert state.selected_models == []

        assert text.index("This machine") < text.index(_MODEL)
        assert text.index(_MODEL) < text.index("Remote hosts")
        assert text.index("Remote hosts") < text.index("remote-a:7b")
        assert f"{_OTHER}  unreachable" in text

    def test_models_and_completions_cover_every_known_host(self) -> None:
        from cli.completer import MakCompleter
        from cli.core.state import LocalHost
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        state = _local_state(
            local_hosts=[LocalHost(url=_REMOTE, models=["remote-a:7b"])]
        )
        state.mode = MODE_CLOUD
        line = "/models "
        texts = [
            row.text
            for row in MakCompleter(state).get_completions(
                Document(line, len(line)), CompleteEvent()
            )
        ]
        assert f"ollama:{_MODEL}" in texts
        assert f"ollama:remote-a:7b@{_REMOTE}" in texts

        handle_command(f"/models ollama:remote-a:7b@{_REMOTE}", state, _console())
        assert state.selected_models == [f"ollama:remote-a:7b@{_REMOTE}"]

    def test_refresh_models_refreshes_inactive_hosts_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import cli.commands as commands_mod
        from cli.core.state import LocalHost

        class FakeRegistry:
            def refresh_now(self, _keys: dict[str, str]) -> Any:
                raise RuntimeError("offline")

        monkeypatch.setattr(commands_mod, "registry", FakeRegistry)
        _install(FakeClient())
        local_mod.set_seams(
            probe_host_fn=lambda _host: _remote_runtime(
                _REMOTE, ("remote-a:7b", "remote-new:3b")
            )
        )
        state = _local_state(
            local_hosts=[LocalHost(url=_REMOTE, models=["remote-a:7b"])]
        )
        console = _console()
        handle_command("/refresh-models", state, console)
        remote = next(h for h in state.local_hosts if h.url == _REMOTE)
        assert remote.models == ["remote-a:7b", "remote-new:3b"]
        assert "+ remote-new:3b" in _output(console)

    def test_planner_on_a_remembered_host_switches_to_it(self) -> None:
        from cli.core.state import LocalHost

        state = _local_state(
            local_hosts=[LocalHost(url=_REMOTE, models=["remote-a:7b"])]
        )
        handle_command(f"/planner ollama:remote-a:7b@{_REMOTE}", state, _console())
        assert state.planner_base_url == _REMOTE
        assert state.local_models == ["remote-a:7b"]
        assert _URL in {h.url for h in state.local_hosts}
