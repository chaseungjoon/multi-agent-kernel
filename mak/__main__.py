"""Command-line entry point: ``python -m mak --task "..."``.

This module is the composition root's thin shell. It parses arguments, loads and
validates configuration, assembles a :class:`~mak.session.Session` from the runtime
collaborators (``mak.bootstrap`` builds the adapter registry; ``mak.planner.llm``
builds the planner backend), drives the init → plan → run → teardown lifecycle, and
maps domain errors to friendly messages and exit codes.

The logic is split into small functions (``parse_args``, ``build_session``,
``main``) so it is testable without spawning a process; ``main`` takes a
``session_builder`` seam so tests can inject a fully-faked session.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from mak.agent_runner.runner import AgentRunner
from mak.agent_runner.sandbox import SandboxConfig, docker_available
from mak.bootstrap import (
    DEFAULT_KEY_ENV,
    agents_from_specs,
    build_registry,
    default_agent_type,
    validate_config,
)
from mak.config import MakConfig, load_config
from mak.core.exceptions import (
    ConfigError,
    MakError,
    PlannerFailedError,
    PlanReviewAborted,
)
from mak.core.logging import SessionLogger
from mak.git_integration.git import GitHelper
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.planner.llm import build_planner_llm
from mak.planner.planner import Planner
from mak.planner.review import display_plan_for_review
from mak.session import Session

SessionBuilder = Callable[
    [argparse.Namespace, MakConfig, "SandboxConfig | None"], Session
]


def load_env_file(path: Path | None = None) -> None:
    """Load ``mak/.env`` (``KEY=VALUE`` lines) into the environment, if present.

    No external dependency. Already-exported variables win (``setdefault``), so an
    explicit ``export`` overrides the file. This makes the documented convention —
    put provider keys in ``mak/.env`` — actually take effect; the agent adapters
    read those keys from the environment at composition time.
    """
    env_path = path or Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the MAK command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="mak",
        description="Multi Agent Kernel — concurrent multi-agent code editing.",
    )
    parser.add_argument(
        "--task", required=True, help="the natural-language task to perform"
    )
    parser.add_argument(
        "--config",
        default="mak/config.yaml",
        help="path to the MAK config YAML (default: mak/config.yaml)",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="working directory to operate on (overrides config session.work_dir)",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        metavar="PROVIDER[:MODEL]",
        help=(
            "set the agent roster from the command line, overriding the config's "
            "'agents' list. Each entry is a provider (anthropic, openai, gemini) "
            "with an optional model, e.g. --models anthropic:claude-opus-4-8 "
            "openai gemini:gemini-3-pro. One model per provider; keys are read from "
            "the usual env vars (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY)."
        ),
    )
    parser.add_argument(
        "--max-agents",
        type=int,
        default=None,
        metavar="N",
        help="how many agents run concurrently (overrides max_concurrent_agents)",
    )
    parser.add_argument(
        "--agent",
        default=None,
        help="agent type to route tasks lacking an explicit one (overrides default)",
    )
    parser.add_argument(
        "--no-review",
        action="store_true",
        help="skip the human-in-the-loop plan review",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="run CLI-type agents inside a Docker sandbox",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase logging verbosity (-v info, -vv debug)",
    )
    return parser.parse_args(argv)


def _planner_api_key(config: MakConfig) -> str | None:
    """Resolve the planner's API key by reusing the matching agent's env var."""
    model = config.planner.model.lower()
    if model.startswith("claude"):
        backend = "anthropic_api"
    elif model.startswith("gemini"):
        backend = "gemini_api"
    elif model.startswith(("gpt", "o1", "o3", "o4")):
        backend = "openai_api"
    else:
        return None
    for agent in config.agents:
        if agent.type == backend and agent.api_key_env:
            return os.environ.get(agent.api_key_env)
    # The planner's provider may not be in the roster (e.g. an OpenAI-only run with
    # the default Claude planner) — fall back to that provider's conventional env var.
    fallback_env = DEFAULT_KEY_ENV.get(backend)
    return os.environ.get(fallback_env) if fallback_env else None


def build_session(
    args: argparse.Namespace,
    config: MakConfig,
    sandbox: SandboxConfig | None = None,
) -> Session:
    """Assemble a ``Session`` and all its collaborators from configuration."""
    work_dir = Path(config.session.work_dir)
    mak_dir = Path(config.session.mak_dir)

    node_store = NodeStore(mak_dir / "node_store")
    lock_table = LockTable(
        persist_path=mak_dir / "lock_table.json",
        default_timeout=config.session.lock_timeout_s,
    )
    registry = build_registry(config, sandbox=sandbox)
    agent_runner = AgentRunner()
    planner = Planner(
        build_planner_llm(config.planner.model, api_key=_planner_api_key(config)),
        max_retries=config.planner.max_retries,
    )
    git_helper = (
        GitHelper(work_dir, commit_prefix=config.git.commit_prefix)
        if config.git.auto_commit
        else None
    )
    logger = SessionLogger(mak_dir / "session.log")

    return Session(
        session_id=f"mak-{int(time.time())}",
        config=config,
        node_store=node_store,
        lock_table=lock_table,
        registry=registry,
        # AgentRunner satisfies the session's loose _Assigner protocol at runtime;
        # the nominal mismatch is the protocol's object-typed params.
        agent_runner=agent_runner,  # type: ignore[arg-type]
        planner=planner,
        git_helper=git_helper,
        logger=logger,
        default_agent_type=args.agent or default_agent_type(config),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    session_builder: SessionBuilder = build_session,
) -> int:
    """Run MAK end-to-end. Returns a process exit code (0 = success)."""
    args = parse_args(argv)
    load_env_file()  # provider keys from mak/.env; exported vars still win
    logging.basicConfig(
        level=(logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)],
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
        if args.work_dir is not None:
            config = replace(
                config, session=replace(config.session, work_dir=args.work_dir)
            )
        if args.models is not None:
            config = replace(config, agents=agents_from_specs(args.models))
        if args.max_agents is not None:
            if args.max_agents < 1:
                raise ConfigError("--max-agents must be at least 1")
            config = replace(
                config,
                session=replace(
                    config.session, max_concurrent_agents=args.max_agents
                ),
            )
        validate_config(config)
    except ConfigError as exc:
        print(f"mak: configuration error: {exc}", file=sys.stderr)
        return 2

    sandbox: SandboxConfig | None = None
    if args.sandbox:
        sandbox = SandboxConfig()
        if not docker_available(sandbox.docker_bin):
            print(
                "mak: --sandbox requires Docker, which was not found on PATH.",
                file=sys.stderr,
            )
            return 2

    try:
        session = session_builder(args, config, sandbox)
        session.initialize()
        session.plan(args.task, review=not args.no_review)
        result = session.run()

        # Cascade loop: after each wave, check whether any committed signature
        # changes broke callers in other files.  If so, surface those as a new
        # plan for the user to review (same UI as the initial plan), then run
        # another wave.  Repeat until no cascades remain or the user declines.
        while True:
            cascade_tasks = session.detect_cascade_tasks()
            if not cascade_tasks:
                break
            print(
                f"\nmak: {len(cascade_tasks)} cascade task(s) detected — "
                "function signatures changed and the following callers need updating.",
                file=sys.stderr,
            )
            if args.no_review:
                print(
                    "mak: --no-review is set; skipping cascade wave. "
                    "Callers may be broken.",
                    file=sys.stderr,
                )
                break
            try:
                cascade_tasks = display_plan_for_review(
                    cascade_tasks,
                    header=(
                        "\n=== CASCADE WAVE ===\n"
                        "Function signatures changed in the previous wave.\n"
                        "The tasks below update affected call sites.\n"
                        "Approve, edit, or abort.\n"
                        "==================="
                    ),
                )
            except PlanReviewAborted:
                print(
                    "mak: cascade wave declined; callers may still be broken.",
                    file=sys.stderr,
                )
                break
            session.install_plan(cascade_tasks)
            result = session.run()

        tests_passed = session.teardown()
    except PlanReviewAborted:
        print("mak: plan review aborted; no changes were made.", file=sys.stderr)
        return 1
    except PlannerFailedError as exc:
        print(f"mak: planning failed: {exc}", file=sys.stderr)
        return 1
    except MakError as exc:
        print(f"mak: {exc}", file=sys.stderr)
        return 1

    print(
        f"mak: {len(result.completed)} completed, "
        f"{len(result.failed)} failed, {len(result.skipped)} skipped, "
        f"{len(result.blocked)} blocked."
    )
    if not result.ok:
        if result.failed:
            print(f"mak: failed tasks: {', '.join(result.failed)}", file=sys.stderr)
            for task_id in result.failed:
                reason = result.failure_reasons.get(task_id)
                if reason:
                    print(f"mak:   - {task_id}: {reason}", file=sys.stderr)
        if result.skipped:
            print(
                f"mak: skipped (an upstream task failed): {', '.join(result.skipped)}",
                file=sys.stderr,
            )
        if result.blocked:
            print(
                f"mak: blocked (stranded, no failed ancestor): "
                f"{', '.join(result.blocked)}",
                file=sys.stderr,
            )
        return 1
    if not tests_passed:
        print("mak: tasks completed but the test suite did not pass.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
