"""Run MAK as an imported library so we get structured plan data directly."""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cli.core.state import CliState

# ── Token counting ─────────────────────────────────────────────────────────────
# Counts input+output tokens across ALL three providers (agent adapters AND the
# planner), so a run on openai:/gemini: reports real usage instead of 0.
_token_counter: list[int] = [0]
_token_counter_installed: bool = False


def _num(value: Any) -> int:
    """Coerce a usage field to int; 0 for None/non-numbers."""
    return int(value) if isinstance(value, (int, float)) else 0


def anthropic_tokens(usage: Any) -> int:
    """Input + output tokens from an Anthropic ``response.usage``."""
    if usage is None:
        return 0
    return _num(getattr(usage, "input_tokens", 0)) + _num(
        getattr(usage, "output_tokens", 0)
    )


def openai_tokens(usage: Any) -> int:
    """Total tokens from an OpenAI ``response.usage`` (prompt+completion fallback)."""
    if usage is None:
        return 0
    total = getattr(usage, "total_tokens", None)
    if total is not None:
        return _num(total)
    return _num(getattr(usage, "prompt_tokens", 0)) + _num(
        getattr(usage, "completion_tokens", 0)
    )


def gemini_tokens(usage: Any) -> int:
    """Total tokens from a Gemini ``response.usage_metadata``."""
    if usage is None:
        return 0
    total = getattr(usage, "total_token_count", None)
    if total is not None:
        return _num(total)
    return _num(getattr(usage, "prompt_token_count", 0)) + _num(
        getattr(usage, "candidates_token_count", 0)
    )


def _count_anthropic() -> None:
    from anthropic.resources.messages import Messages

    orig = Messages.create

    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        response = orig(self, *args, **kwargs)
        _token_counter[0] += anthropic_tokens(getattr(response, "usage", None))
        return response

    Messages.create = counted  # type: ignore[method-assign]


def _count_openai() -> None:
    from openai.resources.chat.completions import Completions

    orig = Completions.create

    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        response = orig(self, *args, **kwargs)
        _token_counter[0] += openai_tokens(getattr(response, "usage", None))
        return response

    Completions.create = counted  # type: ignore[method-assign]


def _count_gemini() -> None:
    from google.genai.models import Models

    orig = Models.generate_content

    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        response = orig(self, *args, **kwargs)
        _token_counter[0] += gemini_tokens(getattr(response, "usage_metadata", None))
        return response

    Models.generate_content = counted  # type: ignore[method-assign]


def install_token_counter() -> None:
    """Patch each provider SDK's call to accumulate input+output tokens.

    Best-effort per provider: a provider whose SDK is not installed (or whose
    internals moved) is skipped without affecting the others.
    """
    global _token_counter_installed
    if _token_counter_installed:
        return
    for patch in (_count_anthropic, _count_openai, _count_gemini):
        try:
            patch()
        except Exception:  # noqa: BLE001 - missing/renamed SDK internals: skip it
            pass
    _token_counter_installed = True


def reset_token_counter() -> None:
    _token_counter[0] = 0


def read_token_counter() -> int:
    return _token_counter[0]


def _apply_state_to_config(config: Any, state: CliState) -> Any:
    """Apply CLI state overrides to a MakConfig (returns a new copy).

    All overrides are in-memory only — this function never writes to
    mak/config.yaml or any other file.
    """
    from mak.bootstrap import agents_from_specs

    if state.work_dir and state.work_dir != ".":
        work_dir = str(Path(state.work_dir).resolve())
        # Anchor mak_dir inside the target work_dir so the node store, logs,
        # and task graph are not accidentally created inside the MAK repo.
        mak_path = Path(config.session.mak_dir)
        mak_dir = str((Path(work_dir) / mak_path).resolve()) if not mak_path.is_absolute() else config.session.mak_dir
        config = replace(config, session=replace(config.session,
            work_dir=work_dir,
            mak_dir=mak_dir,
        ))
    if state.selected_models:
        config = replace(config, agents=agents_from_specs(state.selected_models))
    config = replace(
        config, session=replace(config.session, max_concurrent_agents=state.max_agents)
    )
    return config


def _resolve_planner_api_key(state: CliState) -> str | None:
    model = state.planner_model.lower()
    if model.startswith("claude"):
        return state.api_keys.get("ANTHROPIC_API_KEY")
    if model.startswith("gemini"):
        return state.api_keys.get("GEMINI_API_KEY")
    if model.startswith(("gpt", "o1", "o3", "o4")):
        return state.api_keys.get("OPENAI_API_KEY")
    return None


def build_session(task: str, state: CliState) -> Any:
    """Build a MAK Session from the current CLI state.

    Returned session has been built but NOT yet initialized — call
    ``session.initialize()`` before planning.
    """
    from mak.__main__ import build_session as _build_session
    from mak.__main__ import load_env_file
    from mak.bootstrap import validate_config
    from mak.config import discover_config_path, load_config

    load_env_file()  # ~/.config/mak/.env, then legacy mak/.env; exports win

    # Inject API keys into env so MAK adapters find them.
    import os
    for name, value in state.api_keys.items():
        if value:
            os.environ[name] = value

    config = load_config(state.config_path or discover_config_path())
    config = _apply_state_to_config(config, state)
    validate_config(config)

    # Override the planner model to respect the user's choice.
    config = replace(
        config,
        planner=replace(config.planner, model=state.planner_model),
    )

    # Do NOT pass config=state.config_path here.  mak.__main__.build_session
    # only reads args.agent; passing the file path would create a reference
    # that could be used to write back to mak/config.yaml in the future.
    args = SimpleNamespace(
        task=task,
        work_dir=state.work_dir or ".",
        models=state.selected_models or None,
        max_agents=state.max_agents,
        agent=None,
        no_review=True,
        sandbox=False,
        verbose=0,
    )

    session = _build_session(args, config)
    return session


def plan_in_thread(session: Any, task: str) -> tuple[list[Any], Exception | None]:
    """Call ``session._planner.decompose()`` in a thread; return (subtasks, error)."""
    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            node_ids = session._node_store.list_nodes()
            result["subtasks"] = session._planner.decompose(task, node_ids)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    return result.get("subtasks", []), result.get("error")


def run_session_in_thread(session: Any) -> tuple[Any, Exception | None]:
    """Run ``session.run()`` in a background thread; return (result, error)."""
    holder: dict[str, Any] = {}

    def _target() -> None:
        try:
            holder["result"] = session.run()
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    while t.is_alive():
        time.sleep(0.05)
    t.join()
    return holder.get("result"), holder.get("error")


def get_pre_task_hash(work_dir: str) -> str | None:
    """Return the current HEAD commit hash before a task starts."""
    cwd = work_dir or "."
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_git_diff(work_dir: str, base_hash: str | None = None) -> str:
    """Return a unified diff covering all MAK changes since *base_hash*."""
    cwd = work_dir or "."

    if base_hash:
        try:
            r = subprocess.run(
                ["git", "diff", base_hash, "HEAD"],
                capture_output=True, text=True, cwd=cwd, timeout=15,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    for args in (
        ["git", "diff", "HEAD~1", "HEAD"],
        ["git", "diff", "--cached"],
        ["git", "diff"],
    ):
        try:
            r = subprocess.run(
                args, capture_output=True, text=True, cwd=cwd, timeout=15,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            break
    return ""
