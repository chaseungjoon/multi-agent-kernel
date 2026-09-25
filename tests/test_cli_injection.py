"""Services reach handlers through ``CliState``, never through module globals."""
from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import cli.core.models as models_mod
import cli.local as local_mod
from cli.commands import handle_command
from cli.core.local_seams import LocalSeams
from cli.core.state import CliState
from rich.console import Console

from mak.local import LocalRuntime, OllamaModel
from mak.local.runtime import KIND_OLLAMA
from mak.models import ModelEntry, ModelRegistry
from mak.models.manifest import Manifest, ProviderBlock, save_manifest

_URL = "http://localhost:11434"


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=200, highlight=False), buffer


class _Client:
    def version(self) -> str:
        return "0.5.7"

    def list_models(self, *, timeout: float | None = None) -> list[OllamaModel]:
        return [OllamaModel(name="qwen:7b")]

    def running(self) -> list[str]:
        return []


def _module_state(module: object) -> dict[str, int]:
    """Identity of every module-level binding, to detect any reassignment."""
    return {name: id(value) for name, value in vars(module).items()}


def test_local_with_injected_seams_changes_no_module_state() -> None:
    before = (_module_state(local_mod), _module_state(models_mod))
    runtime = LocalRuntime(
        kind=KIND_OLLAMA, name="Ollama", base_url=_URL, version="0.5.7",
        models=("qwen:7b",),
    )
    seams = LocalSeams(
        discover=lambda: [runtime],
        client_factory=lambda _url: _Client(),  # type: ignore[arg-type,return-value]
        probe_host=lambda _host: None,
    )
    state = CliState(local_seams=seams)
    console, buffer = _console()
    handle_command("/local", state, console)
    handle_command(f"/local url {_URL}", state, console)
    handle_command("/local status", state, console)
    assert "qwen:7b" in buffer.getvalue()
    assert state.local_base_url == _URL
    assert (_module_state(local_mod), _module_state(models_mod)) == before


def _two_endpoint_registry(tmp_path: Path) -> ModelRegistry:
    path = tmp_path / "models.json"
    blocks = {
        endpoint: ProviderBlock(
            fetched_at=datetime(2026, 9, 1, tzinfo=UTC),
            models=(
                ModelEntry(
                    provider=endpoint,
                    endpoint_id=endpoint,
                    model_id="z-ai/glm-5.2",
                    display_name="z-ai/glm-5.2",
                    evaluated=False,
                ),
            ),
        )
        for endpoint in ("openrouter", "nvidia")
    }
    save_manifest(Manifest(providers=blocks), path)
    return ModelRegistry(manifest_path_=path, sources=())


def test_a_bare_id_offered_by_two_endpoints_is_refused_listing_both(
    tmp_path: Path,
) -> None:
    """D25.2's regression guard: the provider is mandatory, never guessed."""
    state = CliState(
        work_dir=str(tmp_path), model_registry=_two_endpoint_registry(tmp_path)
    )
    before = state.planner
    console, buffer = _console()
    handle_command("/planner z-ai/glm-5.2", state, console)
    out = buffer.getvalue()
    assert "openrouter:z-ai/glm-5.2" in out
    assert "nvidia:z-ai/glm-5.2" in out
    assert state.planner == before
