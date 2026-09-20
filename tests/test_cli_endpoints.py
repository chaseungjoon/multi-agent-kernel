"""Wave 22.9/22.10: the ``/endpoint`` surface and the add/edit wizard.

Every test drives the real command through a scripted input seam, so what is
exercised is the flow a user types, not a helper called directly.
"""

from __future__ import annotations

from collections.abc import Iterator

import cli.endpoints.prompts as prompts
import cli.endpoints.wizard as wizard
import pytest
from cli.commands import handle_command
from cli.core.state import CliState
from cli.endpoints.commands import cmd_endpoint
from cli.endpoints.render import MODEL_LIST_CAP, export_yaml
from rich.console import Console

from mak.endpoints.store import endpoints_path, load_user_endpoints
from mak.endpoints.types import EndpointConfig, Location, Transport


@pytest.fixture
def console() -> Console:
    # width kept wide so assertions are not defeated by wrapping
    return Console(width=200, no_color=True, highlight=False)


@pytest.fixture
def state() -> CliState:
    return CliState()


class _Script:
    """Answers the wizard's questions in order, then cancels."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, console: Console, prompt: str, default: str = "") -> str:
        self.asked.append(prompt)
        if not self.answers:
            return prompts.CANCELLED
        return self.answers.pop(0)


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Script]:
    """Install a scripted input seam over every prompt the wizard uses."""
    holder: list[_Script] = []

    def install(answers: list[str]) -> _Script:
        scripted = _Script(answers)
        monkeypatch.setattr(prompts, "ask", scripted)
        monkeypatch.setattr(wizard, "ask", scripted)
        monkeypatch.setattr(
            wizard, "ask_secret", lambda c, p: scripted(c, p)
        )
        holder.append(scripted)
        return scripted

    # Exposed as a callable so each test writes its own script.
    yield install  # type: ignore[misc]


def _save(*endpoints: EndpointConfig) -> None:
    from mak.endpoints.store import save_user_endpoints

    save_user_endpoints(endpoints)


def _endpoint(endpoint_id: str = "gw", **kw: object) -> EndpointConfig:
    base: dict[str, object] = {
        "id": endpoint_id,
        "transport": Transport.OPENAI_CHAT,
        "base_url": "https://gw.example/v1",
        "api_key_env": "GW_KEY",
        "location": Location.HOSTED,
    }
    base.update(kw)
    return EndpointConfig(**base)  # type: ignore[arg-type]


def _output(console: Console) -> str:
    return console.export_text() if console.record else ""


def _run(args: list[str], state: CliState) -> str:
    """Run ``/endpoint <args>`` and return everything it printed."""
    console = Console(width=200, no_color=True, highlight=False, record=True)
    cmd_endpoint(args, state, console)
    return console.export_text()


class TestListing:
    def test_an_empty_store_explains_how_to_add_one(self, state: CliState) -> None:
        out = _run(["list"], state)
        assert "No endpoints configured" in out
        assert "/endpoint add" in out

    def test_list_is_the_default_subcommand(self, state: CliState) -> None:
        _save(_endpoint())
        assert "gw" in _run([], state)

    def test_a_row_shows_id_location_url_and_key_state(
        self, state: CliState
    ) -> None:
        _save(_endpoint())
        out = _run(["list"], state)
        assert "gw" in out
        assert "hosted" in out
        assert "https://gw.example/v1" in out
        assert "GW_KEY: unset" in out

    def test_a_row_never_shows_a_key_value(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GW_KEY", "sk-sentinel")
        _save(_endpoint())
        out = _run(["list"], state)
        assert "sk-sentinel" not in out
        assert "GW_KEY: set" in out

    def test_a_corrupt_store_is_surfaced_not_swallowed(
        self, state: CliState
    ) -> None:
        path = endpoints_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        out = _run(["list"], state)
        assert "could not be read" in out


class TestShowAndExport:
    def test_show_lists_every_non_secret_setting(self, state: CliState) -> None:
        _save(_endpoint())
        out = _run(["show", "gw"], state)
        assert "transport" in out and "openai_chat" in out
        assert "GW_KEY" in out

    def test_show_names_an_unknown_id_and_what_exists(
        self, state: CliState
    ) -> None:
        _save(_endpoint("real"))
        out = _run(["show", "typo"], state)
        assert "No endpoint 'typo'" in out
        assert "real" in out

    def test_show_without_an_id_says_the_usage(self, state: CliState) -> None:
        assert "Usage: /endpoint show" in _run(["show"], state)

    def test_export_is_pasteable_and_secret_free(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GW_KEY", "sk-sentinel")
        yaml_text = export_yaml(_endpoint())
        assert "endpoints:" in yaml_text
        assert "api_key_env: 'GW_KEY'" in yaml_text
        assert "sk-sentinel" not in yaml_text

    def test_export_omits_capabilities_left_to_the_profile(self) -> None:
        """Writing them would freeze today's defaults into a lasting file."""
        yaml_text = export_yaml(_endpoint(profile="nvidia"))
        assert "structured_output" not in yaml_text
        assert "profile: 'nvidia'" in yaml_text


class TestModels:
    def _with_models(self, count: int) -> None:
        from datetime import UTC, datetime

        from mak.models.catalog import ModelEntry
        from mak.models.manifest import Manifest, ProviderBlock, save_manifest

        save_manifest(
            Manifest(
                providers={
                    "gw": ProviderBlock(
                        fetched_at=datetime(2026, 9, 20, tzinfo=UTC),
                        models=tuple(
                            ModelEntry(
                                provider="gw",
                                endpoint_id="gw",
                                model_id=f"model-{i:03d}",
                                display_name=f"model-{i:03d}",
                            )
                            for i in range(count)
                        ),
                    )
                }
            )
        )

    def test_an_endpoint_with_no_cache_says_how_to_get_one(
        self, state: CliState
    ) -> None:
        _save(_endpoint())
        out = _run(["models", "gw"], state)
        assert "No models cached" in out
        assert "/refresh-models" in out

    def test_a_long_list_is_capped_and_asks_for_a_filter(
        self, state: CliState
    ) -> None:
        """Several hundred rows would destroy the session's scrollback."""
        _save(_endpoint())
        self._with_models(MODEL_LIST_CAP + 25)
        from cli.core.models import registry

        registry().reload()
        out = _run(["models", "gw"], state)
        assert "and 25 more" in out
        assert "Narrow it" in out

    def test_a_filter_selects_a_subset(self, state: CliState) -> None:
        _save(_endpoint())
        self._with_models(10)
        from cli.core.models import registry

        registry().reload()
        out = _run(["models", "gw", "model-003"], state)
        assert "model-003" in out
        assert "model-004" not in out

    def test_a_filter_matching_nothing_says_so(self, state: CliState) -> None:
        _save(_endpoint())
        self._with_models(3)
        from cli.core.models import registry

        registry().reload()
        assert "matches" in _run(["models", "gw", "zzz"], state)


class TestRemove:
    def test_removal_is_refused_while_an_agent_uses_it(
        self, state: CliState
    ) -> None:
        _save(_endpoint())
        state.selected_models = ["gw:some-model"]
        out = _run(["remove", "gw"], state)
        assert "still in use" in out
        assert "selected agent models" in out
        assert load_user_endpoints()[0]

    def test_removal_is_refused_while_the_planner_uses_it(
        self, state: CliState
    ) -> None:
        _save(_endpoint())
        state.planner_endpoint_id = "gw"
        out = _run(["remove", "gw"], state)
        assert "still in use" in out
        assert "the planner" in out

    def test_declining_the_confirmation_changes_nothing(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _save(_endpoint())
        monkeypatch.setattr(
            "cli.endpoints.commands.confirm", lambda *a, **k: False
        )
        out = _run(["remove", "gw"], state)
        assert "Cancelled" in out
        assert [e.id for e in load_user_endpoints()[0]] == ["gw"]

    def test_confirming_removes_the_endpoint(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _save(_endpoint())
        answers = iter([True, False])
        monkeypatch.setattr(
            "cli.endpoints.commands.confirm", lambda *a, **k: next(answers)
        )
        out = _run(["remove", "gw"], state)
        assert "removed" in out
        assert load_user_endpoints()[0] == ()

    def test_deleting_the_key_is_a_separate_question(
        self, state: CliState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forgetting a URL does not imply deleting a credential.

        The same variable may serve another endpoint.
        """
        from cli.core.api_keys import load_all_stored, save_keys

        save_keys({"GW_KEY": "sk-keep"})
        _save(_endpoint())
        asked: list[str] = []

        def record(console: object, question: str, **_: object) -> bool:
            asked.append(question)
            return question.startswith("Forget")

        monkeypatch.setattr("cli.endpoints.commands.confirm", record)
        _run(["remove", "gw"], state)
        assert any("Forget" in q for q in asked)
        assert any("GW_KEY" in q for q in asked)
        assert load_all_stored().get("GW_KEY") == "sk-keep"


class TestWizardCancellation:
    """Cancelling anywhere must leave state, the store and .env untouched."""

    @pytest.mark.parametrize("answers", [[], ["nvidia"], ["nvidia", "nv-work"]])
    def test_cancelling_at_any_step_saves_nothing(
        self,
        state: CliState,
        script: object,
        answers: list[str],
    ) -> None:
        script(answers)  # type: ignore[operator]
        out = _run(["add", "nvidia"], state)
        assert "Cancelled" in out
        assert load_user_endpoints()[0] == ()
        assert state.selected_models == []
        assert state.planner_endpoint_id == ""

    def test_cancelling_leaves_the_key_file_untouched(
        self, state: CliState, script: object
    ) -> None:
        from cli.core.api_keys import load_all_stored, save_keys

        save_keys({"OPENAI_API_KEY": "sk-before"})
        script(["nvidia", "nv"])  # type: ignore[operator]
        _run(["add", "nvidia"], state)
        assert load_all_stored() == {"OPENAI_API_KEY": "sk-before"}


class TestWizardValidation:
    def test_a_reserved_id_is_refused_with_a_suggestion(
        self, state: CliState, script: object
    ) -> None:
        script(["openai"])  # type: ignore[operator]
        out = _run(["add", "custom"], state)
        assert "reserved" in out
        assert "openai-gateway" in out

    def test_an_unknown_preset_lists_the_known_ones(
        self, state: CliState
    ) -> None:
        out = _run(["add", "mistral"], state)
        assert "Unknown profile" in out
        assert "nvidia" in out

    def test_an_already_configured_id_is_refused(
        self, state: CliState, script: object
    ) -> None:
        _save(_endpoint("nvidia-work"))
        script(["nvidia-work"])  # type: ignore[operator]
        out = _run(["add", "nvidia"], state)
        assert "already configured" in out


class TestDispatch:
    def test_the_command_is_registered(self, state: CliState) -> None:
        console = Console(width=200, no_color=True, record=True)
        handle_command("/endpoint list", state, console)
        assert "No endpoints configured" in console.export_text()

    def test_an_unknown_subcommand_points_at_help(self, state: CliState) -> None:
        out = _run(["nonsense"], state)
        assert "Unknown /endpoint command" in out
        assert "/endpoint help" in out

    def test_help_lists_every_subcommand(self, state: CliState) -> None:
        out = _run(["help"], state)
        for name in ("list", "add", "show", "edit", "test", "models", "remove"):
            assert name in out

    def test_help_names_the_presets(self, state: CliState) -> None:
        out = _run(["help"], state)
        assert "nvidia" in out and "openrouter" in out


class TestCompletions:
    def _completions(self, text: str, state: CliState) -> list[str]:
        from cli.completer import MakCompleter
        from prompt_toolkit.document import Document

        completer = MakCompleter(state)
        return [
            c.text
            for c in completer.get_completions(Document(text), None)  # type: ignore[arg-type]
        ]

    def test_the_command_completes(self, state: CliState) -> None:
        assert "/endpoint" in self._completions("/end", state)

    def test_subcommands_complete(self, state: CliState) -> None:
        assert "models" in self._completions("/endpoint mo", state)

    def test_add_completes_profiles(self, state: CliState) -> None:
        assert "nvidia" in self._completions("/endpoint add nv", state)

    def test_id_arguments_complete_from_state(self, state: CliState) -> None:
        state.endpoint_ids = ["nvidia-work", "openrouter"]
        assert self._completions("/endpoint show nv", state) == ["nvidia-work"]

    def test_add_does_not_offer_endpoint_ids(self, state: CliState) -> None:
        state.endpoint_ids = ["nvidia-work"]
        assert "nvidia-work" not in self._completions("/endpoint add ", state)
