"""Wave 15 acceptance: local LLM support, end to end.

Real config loading, real composition root, real ``AgentRunner``, real session
wiring — fake clients only at the HTTP boundary, so no test here opens a socket.
This is the test that fails if the wave's contract breaks, independently of the
unit tests: a packaged config must reach a built adapter, both transports must
dispatch, a malformed reply must be repaired, an over-long bundle must be refused
loudly, a local planner must be reachable by name, and the app must start with no
API key anywhere.
"""

from __future__ import annotations

import io
import json
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import cli.local as local_mod
import pytest
from cli.core.state import MODE_LOCAL, CliState
from cli.local import run_wizard
from cli.runner import _apply_state_to_config
from rich.console import Console

from mak.agent_runner.adapters.ollama_api_adapter import OllamaApiAdapter
from mak.agent_runner.adapters.openai_api_adapter import OpenAiApiAdapter
from mak.agent_runner.runner import AgentRunner
from mak.bootstrap import build_registry, validate_config
from mak.config import example_path, load_config
from mak.core.exceptions import AgentContextExceededError
from mak.core.types import NodeId, TaskBundle
from mak.local import LocalRuntime, OllamaChatResponse, OllamaModel, PullProgress
from mak.local.runtime import KIND_OLLAMA
from mak.models.registry import ModelRegistry
from mak.planner.llm import OllamaPlannerLLM, build_planner_llm
from mak.planner.planner import Planner

_URL = "http://localhost:11434"
_MODEL = "qwen2.5-coder:14b"

_RESULT = {
    "task_id": "t1",
    "success": True,
    "modified_fragments": [
        {"node_id": "m.py::function::f", "new_source": "def f():\n    return 1\n"}
    ],
}


def _bundle(task_id: str = "t1") -> TaskBundle:
    return TaskBundle(
        task_id=task_id,
        description="rewrite f",
        target_nodes=[NodeId("m.py::function::f")],
        context={"write_source:m.py::function::f": "def f():\n    return 0\n"},
    )


# ── fakes at the HTTP boundary ────────────────────────────────────────────────


class FakeOllama:
    """An OllamaClient stand-in with a scripted reply queue."""

    def __init__(
        self,
        replies: list[OllamaChatResponse] | None = None,
        *,
        context_length: int = 32768,
        installed: tuple[str, ...] = (_MODEL,),
    ) -> None:
        self._replies = list(replies or [])
        self._context_length = context_length
        self._installed = installed
        self.calls: list[dict[str, Any]] = []

    def version(self) -> str:
        return "0.5.7"

    def list_models(self) -> list[OllamaModel]:
        return [OllamaModel(name=name) for name in self._installed]

    def running(self) -> list[str]:
        return []

    def show(self, model: str) -> OllamaModel:
        return OllamaModel(name=model, context_length=self._context_length)

    def pull(self, model: str) -> Iterator[PullProgress]:
        yield PullProgress(status="success")

    def chat(self, **kwargs: Any) -> OllamaChatResponse:
        self.calls.append(kwargs)
        if self._replies:
            return self._replies.pop(0)
        return _ollama_reply(json.dumps(_RESULT))


def _ollama_reply(
    content: str, *, prompt_tokens: int = 100, output_tokens: int = 10
) -> OllamaChatResponse:
    return OllamaChatResponse(
        content=content,
        done_reason="stop",
        prompt_eval_count=prompt_tokens,
        eval_count=output_tokens,
    )


class FakeOpenAi:
    """A minimal OpenAI-compatible client with a scripted reply queue."""

    def __init__(self, contents: list[str] | None = None) -> None:
        self._contents = list(contents or [json.dumps(_RESULT)])
        self.calls: list[dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        content = self._contents.pop(0) if self._contents else json.dumps(_RESULT)
        message = types.SimpleNamespace(content=content)
        choice = types.SimpleNamespace(message=message, finish_reason="stop")
        usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=10)
        return types.SimpleNamespace(choices=[choice], usage=usage)


# ── 1. Config → registry ──────────────────────────────────────────────────────


def test_the_ollama_example_builds_an_ollama_adapter_at_its_endpoint() -> None:
    config = load_config(example_path("local-ollama"))
    validate_config(config)
    adapter = build_registry(config).get("ollama_api")
    assert isinstance(adapter, OllamaApiAdapter)
    assert adapter.base_url == _URL
    assert adapter.model == _MODEL
    assert adapter.structured_output == "json_schema"
    assert adapter.num_ctx == 32768
    assert adapter.keep_alive == "30m"


def test_the_openai_compatible_example_never_forwards_a_real_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2, end to end: a real OPENAI_API_KEY must never reach a base_url host."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-secret")
    config = load_config(example_path("local-openai-compatible"))
    validate_config(config)
    adapter = build_registry(config).get("local_api")
    assert isinstance(adapter, OpenAiApiAdapter)
    assert adapter.agent_type == "local_api"

    captured: dict[str, Any] = {}

    class FakeOpenAiModule:
        @staticmethod
        def OpenAI(**kwargs: Any) -> object:  # noqa: N802 - mirrors the SDK
            captured.update(kwargs)
            return object()

    monkeypatch.setitem(sys.modules, "openai", FakeOpenAiModule)
    adapter._get_client()
    assert captured["base_url"] == "http://localhost:8000/v1"
    assert captured["api_key"] == "local"
    assert "sk-real-secret" not in json.dumps(captured)


# ── 2. Dispatch, both transports ──────────────────────────────────────────────


def test_the_ollama_transport_dispatches_a_task() -> None:
    adapter = OllamaApiAdapter(client=FakeOllama(), model=_MODEL)
    result = AgentRunner().assign(adapter, _bundle())
    assert result.success is True
    assert result.new_sources == {"m.py::function::f": "def f():\n    return 1\n"}


def test_the_openai_compatible_transport_dispatches_a_task() -> None:
    adapter = OpenAiApiAdapter(
        client=FakeOpenAi(), agent_type="local_api", base_url="http://h:8000/v1"
    )
    result = AgentRunner().assign(adapter, _bundle())
    assert result.success is True
    assert result.new_sources == {"m.py::function::f": "def f():\n    return 1\n"}


# ── 3. Repair, through the one shared loop ────────────────────────────────────


def test_a_prose_reply_is_repaired_in_one_turn_on_the_ollama_transport() -> None:
    client = FakeOllama(
        [
            _ollama_reply("Sure! I rewrote it.", prompt_tokens=900, output_tokens=8),
            _ollama_reply(json.dumps(_RESULT), prompt_tokens=60, output_tokens=40),
        ]
    )
    adapter = OllamaApiAdapter(client=client, model=_MODEL)
    result = AgentRunner().assign(adapter, _bundle())
    assert result.success is True
    assert result.repairs == 1
    # Summed across both turns: max_total_tokens is computed from this.
    assert result.usage == {"input_tokens": 960, "output_tokens": 48}
    assert len(client.calls) == 2


def test_a_prose_reply_is_repaired_in_one_turn_on_the_openai_transport() -> None:
    client = FakeOpenAi(["Sure! I rewrote it.", json.dumps(_RESULT)])
    adapter = OpenAiApiAdapter(
        client=client, agent_type="local_api", base_url="http://h:8000/v1"
    )
    result = AgentRunner().assign(adapter, _bundle())
    assert result.success is True
    assert result.repairs == 1
    assert result.usage == {"input_tokens": 200, "output_tokens": 20}
    assert len(client.calls) == 2


def test_the_repair_turn_reaches_the_session_log(tmp_path: Path) -> None:
    from mak.core.logging import EventType, SessionLogger

    log = tmp_path / "session.log"
    logger = SessionLogger(log)
    client = FakeOllama([_ollama_reply("prose"), _ollama_reply(json.dumps(_RESULT))])
    adapter = OllamaApiAdapter(client=client, model=_MODEL)
    result = AgentRunner().assign(adapter, _bundle())
    logger.log(EventType.AGENT_RESULT, repairs=result.repairs)
    assert '"repairs": 1' in log.read_text()


# ── 4. The context guard ──────────────────────────────────────────────────────


def test_an_over_long_bundle_fails_non_retryably_naming_both_settings() -> None:
    client = FakeOllama(context_length=8192)
    adapter = OllamaApiAdapter(client=client, model=_MODEL, max_tokens=4096)
    huge = TaskBundle(
        task_id="t1",
        description="rewrite everything",
        target_nodes=[NodeId("m.py")],
        context={"write_source:m.py": "x = 1\n" * 60_000},
    )
    result = AgentRunner().assign(adapter, huge)

    assert result.success is False
    assert result.retryable is False
    assert result.error_kind == "context"
    assert result.error is not None
    assert "dependency_context_bytes" in result.error
    assert "cross_file_context_bytes" in result.error
    # Refused before the call: nothing was truncated and answered from.
    assert client.calls == []


def test_the_context_error_is_classified_as_non_retryable() -> None:
    assert AgentContextExceededError("x").retryable is False
    assert AgentContextExceededError("x").kind == "context"


# ── 5. The planner over a local runtime ───────────────────────────────────────


def test_a_local_planner_resolves_by_name_and_produces_a_plan() -> None:
    llm = build_planner_llm(_MODEL, backend="ollama", base_url=_URL)
    assert isinstance(llm, OllamaPlannerLLM)

    plan = json.dumps(
        {
            "subtasks": [
                {
                    "task_id": "t1",
                    "description": "rewrite f",
                    "target_nodes": ["m.py::function::f"],
                    "context_nodes": [],
                    "depends_on": [],
                    "agent_type": "ollama_api",
                }
            ]
        }
    )
    llm._client = FakeOllama([_ollama_reply(plan)])  # type: ignore[assignment]
    planner = Planner(llm, agent_types=["ollama_api"])
    tasks = planner.decompose("rewrite f", [NodeId("m.py::function::f")])
    assert [t.task_id for t in tasks] == ["t1"]
    assert tasks[0].target_nodes == [NodeId("m.py::function::f")]


# ── 6. The app, with no API key anywhere ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_local_seams() -> Iterator[None]:
    yield
    local_mod.reset_seams()


def test_the_app_configures_a_local_run_with_no_key_and_no_cloud_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    runtime = LocalRuntime(
        kind=KIND_OLLAMA,
        name="Ollama",
        base_url=_URL,
        version="0.5.7",
        models=(_MODEL,),
    )
    local_mod.set_seams(
        discover_fn=lambda: [runtime],
        client_factory=lambda _url: FakeOllama(),  # type: ignore[arg-type,return-value]
    )
    # model 1 · planner "the same local model" · do not save
    replies = iter(["1", "1", "n"])
    monkeypatch.setattr(
        local_mod, "_ask", lambda _c, _p, default="": next(replies, default)
    )

    state = CliState()
    console = Console(file=io.StringIO(), highlight=False, width=100)
    assert run_wizard(state, console) is True
    assert state.mode == MODE_LOCAL
    assert state.api_keys == {}

    config = _apply_state_to_config(load_config(example_path("local-ollama")), state)
    validate_config(config)
    assert [a.type for a in config.agents] == ["ollama_api"]
    assert config.agents[0].base_url == _URL

    # No cloud call anywhere: with no keys, the catalog refresh declines.
    assert ModelRegistry(manifest_path_=tmp_path / "models.json").maybe_auto_refresh(
        {}
    ) is False


def test_app_startup_does_not_exit_without_a_key() -> None:
    """Structural: the `sys.exit(1)` on "no key set" is gone (D12)."""
    import inspect

    from cli.app import MakCli

    source = inspect.getsource(MakCli.run)
    assert "has_local_runtime" in source
    assert source.count("sys.exit") == 1


# ── 7. The startup preflight names the real reason ────────────────────────────


class _DownOllama(FakeOllama):
    def version(self) -> str:
        from mak.local import OllamaError

        raise OllamaError(f"cannot reach {_URL}/api/version: Connection refused")


def test_a_stopped_server_is_reported_once_at_startup_with_the_reason() -> None:
    from mak.bootstrap import healthy_agent_types

    config = load_config(example_path("local-ollama"))
    registry = build_registry(config)
    adapter = registry.get("ollama_api")
    assert isinstance(adapter, OllamaApiAdapter)
    adapter._client = _DownOllama()  # type: ignore[assignment]
    registry.replace_factory("ollama_api", lambda: adapter)

    healthy, unhealthy, why = healthy_agent_types(registry, ["ollama_api"])
    assert healthy == []
    assert unhealthy == ["ollama_api"]
    # Not the generic "missing API key/SDK, or CLI not on PATH", which would
    # send a local user to fix the wrong thing.
    assert "Ollama is not running" in why["ollama_api"]
    assert _URL in why["ollama_api"]


def test_an_unpulled_model_gets_its_own_reason() -> None:
    from mak.bootstrap import healthy_agent_types

    config = load_config(example_path("local-ollama"))
    registry = build_registry(config)
    adapter = registry.get("ollama_api")
    assert isinstance(adapter, OllamaApiAdapter)
    adapter._client = FakeOllama(installed=("llama3.1:8b",))  # type: ignore[assignment]
    registry.replace_factory("ollama_api", lambda: adapter)

    _healthy, unhealthy, why = healthy_agent_types(registry, ["ollama_api"])
    assert unhealthy == ["ollama_api"]
    assert "is not pulled" in why["ollama_api"]
    assert f"ollama pull {_MODEL}" in why["ollama_api"]


# ── 8. The command line ───────────────────────────────────────────────────────


def test_models_ollama_on_the_command_line_yields_a_local_roster() -> None:
    from dataclasses import replace as dc_replace

    from mak.__main__ import parse_args
    from mak.bootstrap import agents_from_specs
    from mak.config import MakConfig

    args = parse_args(["--task", "t", "--models", f"ollama:{_MODEL}"])
    config = dc_replace(MakConfig(), agents=agents_from_specs(args.models))
    validate_config(config)
    assert [a.type for a in config.agents] == ["ollama_api"]
    assert config.agents[0].base_url == _URL
    assert config.agents[0].api_key_env is None
    assert isinstance(build_registry(config).get("ollama_api"), OllamaApiAdapter)


def test_models_local_with_an_endpoint_yields_an_openai_compatible_roster() -> None:
    from dataclasses import replace as dc_replace

    from mak.__main__ import parse_args
    from mak.bootstrap import agents_from_specs
    from mak.config import MakConfig

    args = parse_args(
        ["--task", "t", "--models", "local:my-model@http://localhost:8000/v1"]
    )
    config = dc_replace(MakConfig(), agents=agents_from_specs(args.models))
    validate_config(config)
    adapter = build_registry(config).get("local_api")
    assert isinstance(adapter, OpenAiApiAdapter)
    assert adapter.agent_type == "local_api"
    assert adapter.base_url == "http://localhost:8000/v1"
