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
    healthy_agent_types,
    validate_config,
)
from mak.config import (
    MakConfig,
    discover_config_path,
    load_config,
    model_caveat,
    user_config_dir,
)
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
from mak.session import Session, SessionState
from mak.test_runner import build_test_runner

SessionBuilder = Callable[
    [argparse.Namespace, MakConfig, "SandboxConfig | None"], Session
]


def _load_one_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def load_env_file(path: Path | None = None) -> None:
    """Load MAK ``.env`` files (``KEY=VALUE`` lines) into the environment.

    No external dependency. Already-exported variables win (``setdefault``), so an
    explicit ``export`` overrides any file. With an explicit ``path`` only that
    file is read; otherwise two locations are tried, earlier ones winning:

    1. ``<user config dir>/.env`` (e.g. ``~/.config/mak/.env``) — where an
       installed MAK's ``/apikey`` setup stores keys.
    2. ``mak/.env`` next to this module — the legacy source-checkout location.
    """
    if path is not None:
        _load_one_env_file(path)
        return
    _load_one_env_file(user_config_dir() / ".env")
    _load_one_env_file(Path(__file__).resolve().parent / ".env")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the MAK command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="mak",
        description="Multi Agent Kernel — concurrent multi-agent code editing.",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="the natural-language task to perform (required unless --recover)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "path to the MAK config YAML (default: auto-discover ./mak.yaml, "
            "then ~/.config/mak/config.yaml, then the built-in default)"
        ),
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
            "openai gemini:gemini-3.5-flash. One model per provider; keys are read "
            "from the usual env vars (ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "GEMINI_API_KEY)."
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
        "--recover",
        action="store_true",
        help=(
            "resume a crashed session from .mak/task_graph.json instead of "
            "planning afresh (expires stale locks, re-queues in-flight tasks)"
        ),
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


def warn_model_caveats(config: MakConfig) -> None:
    """Print one stderr warning per caveated model in the run's configuration.

    Covers every way a model can be chosen for a run — the planner and each
    agent, whether set via config file or ``--models``. Deduplicated so a run
    whose planner and agents share a model warns once.
    """
    models = [config.planner.model, *(a.model for a in config.agents)]
    seen: set[str] = set()
    for model in models:
        caveat = model_caveat(model)
        if caveat and caveat not in seen:
            seen.add(caveat)
            print(f"mak: warning: {caveat}", file=sys.stderr)


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
    # Health preflight: verify each configured agent is usable *before* dispatch,
    # so a missing CLI binary or absent API key surfaces now instead of as a
    # mid-run failure or a long timeout. The healthy set becomes the distribution
    # pool; the default agent must be among it.
    default_type = args.agent or default_agent_type(config)
    configured = [a.type for a in config.agents]
    healthy, unhealthy = healthy_agent_types(registry, configured)
    for agent_type in unhealthy:
        print(
            f"mak: warning: agent '{agent_type}' failed its health check "
            "(missing API key/SDK, or CLI not on PATH) — it will not be used.",
            file=sys.stderr,
        )
    if default_type not in healthy:
        raise ConfigError(
            f"the default agent '{default_type}' is not usable "
            "(failed its health check); configure a working agent/key"
        )
    # Per-agent config knobs reach the runner here: the read timeout is the
    # largest configured agent timeout (so no agent is cut short), and each
    # agent type's max_instances caps its retained idle subprocess pool.
    agent_runner = AgentRunner(
        timeout_s=max((a.timeout for a in config.agents), default=300),
        pool_caps={a.type: a.max_instances for a in config.agents},
    )
    planner = Planner(
        build_planner_llm(config.planner.model, api_key=_planner_api_key(config)),
        max_retries=config.planner.max_retries,
        agent_types=healthy,
        strategy=config.planner.strategy,
        self_critique=config.planner.self_critique,
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
        test_runner=build_test_runner(config.session.test_command, work_dir),
        default_agent_type=default_type,
        agent_pool=healthy,
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
        config = load_config(args.config or discover_config_path())
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
        if not args.recover and not args.task:
            raise ConfigError("--task is required (or use --recover to resume)")
        validate_config(config)
    except ConfigError as exc:
        print(f"mak: configuration error: {exc}", file=sys.stderr)
        return 2
    warn_model_caveats(config)

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
        if args.recover:
            session.recover()
            if session.state is not SessionState.PLANNED:
                print(
                    "mak: nothing to recover — no .mak/task_graph.json for this "
                    "work dir. Run a normal task instead.",
                    file=sys.stderr,
                )
                return 1
            print(
                "mak: resuming the previous session from .mak/task_graph.json.",
                file=sys.stderr,
            )
        else:
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

    # "N completed" used to include tasks where the agent changed nothing, with
    # no way for an operator to tell the two apart — so a task that declined to
    # do work is counted, but named.
    noop_note = f" ({len(result.noop)} no-op)" if result.noop else ""
    print(
        f"mak: {len(result.completed)} completed{noop_note}, "
        f"{len(result.failed)} failed, {len(result.skipped)} skipped, "
        f"{len(result.blocked)} blocked."
    )
    if result.noop:
        print(
            "mak: no-op (the agent reported nothing needed changing): "
            f"{', '.join(result.noop)}"
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
