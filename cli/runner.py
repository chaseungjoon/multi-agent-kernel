"""Run MAK as an imported library so we get structured plan data directly."""
from __future__ import annotations

import argparse
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli.core.state import CliState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mak.config import MakConfig
    from mak.session import Session

# ── Token counting ─────────────────────────────────────────────────────────────
# Read from what each provider reported on its own response (Session.token_usage),
# which covers all three providers plus the planner.
#
# This replaces three SDK monkeypatches that wrapped Messages.create /
# Completions.create / Models.generate_content. They were wrong as well as
# fragile: the Anthropic agent adapter and the Anthropic planner both call
# `messages.stream`, which never routes through `Messages.create` — so the
# counter reported a flat zero for MAK's default provider, and the tests only
# ever exercised the pure helpers, never the patch point.


def session_tokens(session: Session) -> int:
    """Input + output tokens a finished MAK session spent, agents and planner."""
    total = getattr(session, "total_tokens", 0)
    return total if isinstance(total, int) else 0


def _apply_state_to_config(config: MakConfig, state: CliState) -> MakConfig:
    """Apply CLI state overrides to a MakConfig (returns a new copy).

    All overrides are in-memory only — this function never writes to
    mak/config.yaml or any other file.
    """
    from mak.bootstrap import agents_from_specs
    from mak.config import anchor_mak_dir

    if state.work_dir and state.work_dir != ".":
        config = replace(
            config,
            session=replace(
                config.session, work_dir=str(Path(state.work_dir).resolve())
            ),
        )
    # Anchor the node store, logs, and task graph inside the work dir. Shared with
    # `mak run` (mak.config.anchor_mak_dir) rather than reimplemented here: the two
    # front ends previously disagreed about where a project's state lives, and only
    # this one was right.
    config = anchor_mak_dir(config)
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


def build_session(task: str, state: CliState) -> Session:
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
    # A real argparse.Namespace, not a SimpleNamespace lookalike: mak's
    # build_session is typed against Namespace, and a duck-typed stand-in
    # silently breaks the TUI the day build_session reads a new attribute.
    args = argparse.Namespace(
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


def plan_in_thread(
    session: Session, task: str
) -> tuple[list[Any], Exception | None]:
    """Call ``session._planner.decompose()`` in a thread; return (subtasks, error)."""
    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            planner = session._planner
            if planner is None:
                # Latent until the gate was extended to cli/: build_session always
                # configures a planner today, so this only ever surfaced as a bare
                # AttributeError deep in a worker thread if that ever changed.
                raise RuntimeError(
                    "no planner is configured for this session; set a planner "
                    "model with /planner before running a task"
                )
            node_ids = session._node_store.list_nodes()
            result["subtasks"] = planner.decompose(task, node_ids)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    return result.get("subtasks", []), result.get("error")


def run_session_in_thread(session: Session) -> tuple[Any, Exception | None]:
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
