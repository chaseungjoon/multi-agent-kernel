"""Run MAK as an imported library so we get structured plan data directly.

The app goes through the same application API as ``mak run``
(:mod:`mak.application`): it describes the run as a ``RunRequest``, and the
config, the planner route and the planner key are resolved exactly as they are
for the command line. It never writes ``os.environ``; the session's keys reach
the kernel as an explicit ``env`` mapping.
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli.core.state import CliState
from mak.application import (
    RunRequest,
    build_config,
    read_env_files,
)
from mak.application import (
    build_session as build_app_session,
)

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


def request_from_state(state: CliState) -> RunRequest:
    """Describe the run the app's current settings ask for.

    The work dir is always explicit (the one the toolbar shows), the planner is
    the session's route, and the roster is ``selected_models`` when one was
    chosen — honored even when it does not match the mode: ``/mode`` offers to
    change a mismatched combination, and a user who declines has chosen it.
    """
    return RunRequest(
        config_path=Path(state.config_path) if state.config_path else None,
        work_dir=str(Path(state.work_dir or ".").resolve()),
        model_specs=tuple(state.selected_models),
        planner=state.planner,
        max_agents=state.max_agents,
        api_keys={name: value for name, value in state.api_keys.items() if value},
        no_review=state.no_review,
    )


def session_env(state: CliState) -> dict[str, str]:
    """Return the environment a session built from ``state`` reads keys from.

    MAK's ``.env`` files, then the process environment (an export beats a
    stored key), then the keys entered in this session — without exporting
    any of them.
    """
    return {
        **read_env_files(),
        **os.environ,
        **{name: value for name, value in state.api_keys.items() if value},
    }


def config_for(state: CliState) -> MakConfig:
    """Build the ``MakConfig`` the next run would use (in memory only)."""
    return build_config(request_from_state(state), env=session_env(state))


def build_session(task: str, state: CliState) -> Session:
    """Build a MAK Session from the current CLI state.

    Returned session has been built but NOT yet initialized — call
    ``session.initialize()`` before planning. ``task`` is accepted for the
    caller's symmetry with ``mak run``; planning happens later.
    """
    request = request_from_state(state)
    env = session_env(state)
    config = build_config(request, env=env)
    return build_app_session(config, env=env, default_agent=request.default_agent)


def plan_in_thread(
    session: Session, task: str
) -> tuple[list[Any], Exception | None]:
    """Propose a plan for ``task`` in a thread; return (subtasks, error)."""
    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            result["subtasks"] = session.propose_plan(task).subtasks
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    return result.get("subtasks", []), result.get("error")


def run_session_in_thread(session: Session) -> tuple[Any, Exception | None]:
    """Run ``session.run()`` in a background thread; return (result, error).

    The join is the whole wait. A ``while t.is_alive(): sleep(0.05)`` spin used to
    precede it, which changed nothing about when this function returned — the
    ``join()`` after it did all the waiting — and burned a core for the length of
    every run. Progress reporting, if it is ever wanted here, belongs on the
    session's log events, not on a polling loop.
    """
    holder: dict[str, Any] = {}

    def _target() -> None:
        try:
            holder["result"] = session.run()
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
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
