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
_token_counter: list[int] = [0]
_token_counter_installed: bool = False


def install_token_counter() -> None:
    """Patch anthropic.resources.messages.Messages.create to count all tokens."""
    global _token_counter_installed
    if _token_counter_installed:
        return
    try:
        from anthropic.resources.messages import Messages

        _orig_create = Messages.create

        def _counted_create(self: Any, *args: Any, **kwargs: Any) -> Any:
            response = _orig_create(self, *args, **kwargs)
            usage = getattr(response, "usage", None)
            if usage is not None:
                _token_counter[0] += (
                    getattr(usage, "input_tokens", 0)
                    + getattr(usage, "output_tokens", 0)
                )
            return response

        Messages.create = _counted_create  # type: ignore[method-assign]
        _token_counter_installed = True
    except Exception:
        pass


def reset_token_counter() -> None:
    _token_counter[0] = 0


def read_token_counter() -> int:
    return _token_counter[0]


def _apply_state_to_config(config: Any, state: CliState) -> Any:
    """Apply CLI state overrides to a MakConfig (returns a new copy)."""
    from mak.bootstrap import agents_from_specs

    if state.work_dir and state.work_dir != ".":
        config = replace(config, session=replace(config.session, work_dir=state.work_dir))
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
    from mak.__main__ import build_session as _build_session, load_env_file
    from mak.bootstrap import validate_config
    from mak.config import load_config

    load_env_file(Path("mak/.env") if Path("mak/.env").exists() else None)

    # Inject API keys into env so MAK adapters find them.
    import os
    for name, value in state.api_keys.items():
        if value:
            os.environ[name] = value

    config = load_config(state.config_path)
    config = _apply_state_to_config(config, state)
    validate_config(config)

    # Override the planner model to respect the user's choice.
    config = replace(
        config,
        planner=replace(config.planner, model=state.planner_model),
    )

    args = SimpleNamespace(
        task=task,
        config=state.config_path,
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
