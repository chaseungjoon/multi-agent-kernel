"""Command-line entry point: ``python -m mak --task "..."``.

A thin shell over the application API (:mod:`mak.application`): it parses
arguments into a :class:`~mak.application.RunRequest`, builds the config and the
session exactly as the interactive app does, drives the init → plan → run →
teardown lifecycle, and maps domain errors to friendly messages and exit codes.

``main`` takes a ``session_builder`` seam so tests can inject a fully-faked
session.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from mak.agent_runner.sandbox import SandboxConfig, docker_available
from mak.application import (
    RunRequest,
    build_config,
    build_session,
    load_env_file,
    planner_endpoint,
)
from mak.bootstrap import resolved_agents
from mak.cascade import CascadeApproval, run_cascade_waves
from mak.config import MakConfig, anchor_mak_dir, model_caveat, stale_mak_dir
from mak.core.exceptions import (
    ConfigError,
    MakError,
    PlannerFailedError,
    PlanReviewAborted,
)
from mak.core.types import SubTask
from mak.execution_result import ExecutionResult
from mak.planner.review import display_plan_for_review
from mak.session import Session, SessionState
from mak.teardown import SuiteOutcome, TeardownResult

SessionBuilder = Callable[
    [argparse.Namespace, MakConfig, "SandboxConfig | None"], Session
]


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
            "path to the MAK config YAML (default: auto-discover "
            "<work dir>/.mak/config.yaml, then ~/.config/mak/config.yaml, then the "
            "built-in default)"
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
        metavar="PROVIDER[:MODEL][@URL]",
        help=(
            "set the agent roster from the command line, overriding the config's "
            "'agents' list. Each entry is provider[:model][@base_url]. Hosted: "
            "anthropic, openai, gemini — e.g. --models anthropic:claude-opus-4-8 "
            "openai gemini:gemini-3.5-flash, with keys read from the usual env "
            "vars (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY). Local: "
            "ollama:<model> (defaults to http://localhost:11434) and "
            "local:<model>@<url> for any OpenAI-compatible server, neither of "
            "which needs a key. One model per provider."
        ),
    )
    parser.add_argument(
        "--planner",
        default=None,
        metavar="PROVIDER:MODEL[@URL]",
        help=(
            "set the planner model from the command line, overriding the "
            "config's 'planner' route. Same grammar as --models, but the model "
            "is required — e.g. --planner anthropic:claude-opus-5, "
            "--planner openrouter:anthropic/claude-opus-5, "
            "--planner ollama:qwen2.5-coder:14b"
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


def _announce_cascade(tasks: list[SubTask]) -> None:
    """Tell the operator a fix-up wave was detected, before asking to run it."""
    print(
        f"\nmak: {len(tasks)} cascade task(s) detected — the files below call "
        "or import something that does not match what it now is.",
        file=sys.stderr,
    )


def _cli_cascade_approval(*, no_review: bool) -> CascadeApproval:
    """Review a cascade plan in the terminal, or decline it under --no-review."""
    def approve(tasks: list[SubTask]) -> list[SubTask] | None:
        if no_review:
            print(
                "mak: --no-review is set; skipping cascade wave. "
                "Callers may be broken.",
                file=sys.stderr,
            )
            return None
        try:
            return display_plan_for_review(
                tasks,
                header=(
                    "\n=== CASCADE WAVE ===\n"
                    "The previous wave left call sites or imports that do "
                    "not resolve.\n"
                    "The tasks below reconcile them.\n"
                    "Approve, edit, or abort.\n"
                    "==================="
                ),
            )
        except PlanReviewAborted:
            print(
                "mak: cascade wave declined; callers may still be broken.",
                file=sys.stderr,
            )
            return None

    return approve


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


def warn_local_planner_mismatch(config: MakConfig) -> None:
    """Warn when every agent is local but the planner still calls a hosted API.

    ``--models ollama:qwen2.5-coder:14b`` looks fully local and quietly is not:
    the planner keeps whatever model the config named, so the run still ships
    the whole node inventory to a hosted provider. For someone choosing local
    models for privacy or for an air-gapped repo, that is the entire point of
    the feature failing silently.

    Hybrid is a legitimate configuration — a cloud planner with local agents is
    the cost/privacy sweet spot — so this is a warning naming the settings that
    change it, not an error.
    """
    if not config.agents:
        return
    try:
        roster = resolved_agents(config)
    except ConfigError:
        # A config that will not resolve has a louder problem than this warning.
        return
    # Reads each agent's resolved location, not its type. The old test used a
    # fixed set of "local" agent types, so a roster of hosted compatible
    # endpoints — NVIDIA, OpenRouter — counted as not-local and the warning
    # never fired; worse, a local endpoint configured the new way would not
    # have matched either.
    if any(not agent.is_local for agent in roster):
        return
    route = planner_endpoint(config)
    if route is not None and not route.is_hosted:
        return
    if route is None and (
        config.planner.backend == "ollama" or config.planner.base_url is not None
    ):
        return
    print(
        "mak: warning: every agent is local, but the planner "
        f"('{config.planner.model or 'unset'}') is not — this run still sends "
        "the node inventory to a hosted API. Set planner.backend (and "
        "planner.base_url for a local server) to plan locally too, or ignore "
        "this if a cloud planner with local agents is what you want.",
        file=sys.stderr,
    )


def _session_from_args(
    args: argparse.Namespace,
    config: MakConfig,
    sandbox: SandboxConfig | None = None,
) -> Session:
    """Build the run's session through the application API."""
    return build_session(
        config, env=os.environ, sandbox=sandbox, default_agent=args.agent
    )


def request_from_args(args: argparse.Namespace) -> RunRequest:
    """Describe the run ``args`` asks for as a :class:`RunRequest`."""
    return RunRequest(
        config_path=Path(args.config) if args.config else None,
        work_dir=args.work_dir,
        model_specs=tuple(args.models or ()),
        planner=args.planner,
        max_agents=args.max_agents,
        default_agent=args.agent,
        sandbox=args.sandbox,
        verbose=args.verbose,
        no_review=args.no_review,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    session_builder: SessionBuilder = _session_from_args,
) -> int:
    """Run MAK end-to-end. Returns a process exit code (0 = success)."""
    args = parse_args(argv)
    load_env_file()  # provider keys from ~/.config/mak/.env; exports still win
    logging.basicConfig(
        level=(logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)],
        format="%(levelname)s %(name)s: %(message)s",
    )

    orphan: Path | None = None
    try:
        if not args.recover and not args.task:
            raise ConfigError("--task is required (or use --recover to resume)")
        config = build_config(request_from_args(args), anchor=False)
        # Anchor last, once work_dir is final: a project's node store, task
        # graph, and log belong with the project, not with whatever directory
        # the operator happened to launch from. Checked for an orphan first,
        # because after anchoring the old location is simply ignored.
        orphan = stale_mak_dir(config)
        config = anchor_mak_dir(config)
    except ConfigError as exc:
        print(f"mak: configuration error: {exc}", file=sys.stderr)
        return 2
    if orphan is not None:
        print(
            f"mak: ignoring the MAK state directory at {orphan} — it was left by an "
            "older version that stored state beside the shell rather than beside "
            f"the project. This run uses {config.session.mak_dir}. Delete the old "
            "one when you no longer need it.",
            file=sys.stderr,
        )
    warn_model_caveats(config)
    warn_local_planner_mismatch(config)

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

        # Cascade loop: after each wave, check whether the wave broke callers —
        # a committed signature change that existing code still calls the old
        # way, or two modules the wave created that disagree about each other's
        # API.  If so, surface those as a new plan for the user to review (same
        # UI as the initial plan), then run another wave.  The loop itself lives
        # in mak.cascade so the interactive app runs exactly the same one.
        cascade = run_cascade_waves(
            session,
            _cli_cascade_approval(no_review=args.no_review),
            announce=_announce_cascade,
        )
        # The aggregate, not the last wave. Overwriting ``result`` with the
        # cascade's result reported an initial wave's failures as if a later
        # successful wave had answered them.
        execution = ExecutionResult(initial=result, cascade=cascade)
        teardown = session.teardown(execution)
    except PlanReviewAborted:
        print("mak: plan review aborted; no changes were made.", file=sys.stderr)
        return 1
    except PlannerFailedError as exc:
        print(f"mak: planning failed: {exc}", file=sys.stderr)
        return 1
    except MakError as exc:
        print(f"mak: {exc}", file=sys.stderr)
        return 1

    return _report(execution, teardown)


def _report(execution: ExecutionResult, teardown: TeardownResult) -> int:
    """Print the run's outcome and return the process exit code."""
    # "N completed" used to include tasks where the agent changed nothing, with
    # no way for an operator to tell the two apart — so a task that declined to
    # do work is counted, but named.
    print(f"mak: {execution.summary_line()}.")
    if execution.noop:
        print(
            "mak: no-op (the agent reported nothing needed changing): "
            f"{_names(execution.noop)}"
        )
    if not execution.request_satisfied:
        for index, reason in execution.stopped_reasons:
            print(
                f"mak: wave {index + 1} stopped — {reason}.", file=sys.stderr
            )
        if execution.failed:
            print(f"mak: failed tasks: {_names(execution.failed)}", file=sys.stderr)
            for key in execution.failed:
                why = execution.failure_reasons.get(key)
                if why:
                    print(f"mak:   - {_name(key)}: {why}", file=sys.stderr)
        if execution.skipped:
            print(
                f"mak: skipped (an upstream task failed): "
                f"{_names(execution.skipped)}",
                file=sys.stderr,
            )
        if execution.blocked:
            print(
                f"mak: blocked (stranded, no failed ancestor): "
                f"{_names(execution.blocked)}",
                file=sys.stderr,
            )
        # The ways a cascade stops without finishing. Each used to be
        # invisible: the loop returned its last successful result either way.
        if execution.cascade.declined:
            print(
                "mak: a cascade wave was declined — callers may still be broken.",
                file=sys.stderr,
            )
        if execution.cascade.limit_reached:
            print(
                "mak: the cascade stopped at its wave limit with defects "
                "remaining.",
                file=sys.stderr,
            )
        if execution.cascade.stalled:
            print(
                f"mak: the cascade stopped without progress — "
                f"{execution.cascade.stop_reason}.",
                file=sys.stderr,
            )
        if execution.cascade.oscillating:
            print(
                f"mak: the cascade stopped on an oscillation — "
                f"{execution.cascade.stop_reason}.",
                file=sys.stderr,
            )
        if execution.cascade.unrepairable:
            print(
                f"mak: the cascade plan cannot satisfy its repair contract — "
                f"{execution.cascade.stop_reason}.",
                file=sys.stderr,
            )
        if execution.unresolved:
            print(
                f"mak: unresolved cascade defects: "
                f"{', '.join(execution.unresolved)}",
                file=sys.stderr,
            )
        return 1
    if teardown.outcome is SuiteOutcome.SKIPPED:
        print("mak: no test_command configured — no suite ran.", file=sys.stderr)
    elif teardown.outcome is SuiteOutcome.ERROR:
        print(f"mak: the test runner errored: {teardown.output}", file=sys.stderr)
        return 1
    elif teardown.outcome is SuiteOutcome.FAILED:
        print("mak: tasks completed but the test suite did not pass.", file=sys.stderr)
        return 1
    if teardown.push_skipped_reason:
        print(f"mak: {teardown.push_skipped_reason}.", file=sys.stderr)
    return 0


def _name(key: tuple[int, str]) -> str:
    """Render an aggregate task key, naming its wave only when there was one."""
    wave, task_id = key
    return task_id if wave == 0 else f"{task_id} (wave {wave + 1})"


def _names(keys: tuple[tuple[int, str], ...]) -> str:
    return ", ".join(_name(k) for k in keys)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
