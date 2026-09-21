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
    default_agent_id,
    healthy_agent_ids,
    resolved_agents,
    validate_config,
)
from mak.cascade import CascadeApproval, run_cascade_waves
from mak.config import (
    MakConfig,
    anchor_mak_dir,
    discover_config_path,
    load_config,
    model_caveat,
    stale_mak_dir,
    user_config_dir,
)
from mak.core.exceptions import (
    ConfigError,
    MakError,
    PlannerFailedError,
    PlanReviewAborted,
)
from mak.core.logging import SessionLogger
from mak.core.types import SubTask
from mak.endpoints.resolution import (
    ResolvedEndpoint,
    require_endpoint,
    resolve_endpoints,
)
from mak.endpoints.store import load_user_endpoints, merge_endpoints
from mak.execution_result import ExecutionResult
from mak.git_integration.git import GitHelper
from mak.lock_manager.lock_table import LockTable
from mak.lock_manager.project_lease import ProjectLease
from mak.models.registry import ReportedCapabilities
from mak.node_store.store import NodeStore
from mak.planner.llm import build_planner_llm
from mak.planner.planner import Planner
from mak.planner.review import display_plan_for_review
from mak.session import Session, SessionState
from mak.teardown import SuiteOutcome, TeardownResult
from mak.test_runner import build_test_runner

SessionBuilder = Callable[
    [argparse.Namespace, MakConfig, "SandboxConfig | None"], Session
]


def _legacy_env_path() -> Path:
    """Return the deprecated in-package ``.env`` location (``mak/.env``)."""
    return Path(__file__).resolve().parent / ".env"


def _load_one_env_file(env_path: Path) -> bool:
    """Load one ``KEY=VALUE`` file into the environment; report if it existed."""
    if not env_path.exists():
        return False
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
    return True


def load_env_file(path: Path | None = None) -> None:
    """Load MAK ``.env`` files (``KEY=VALUE`` lines) into the environment.

    No external dependency. Already-exported variables win (``setdefault``), so an
    explicit ``export`` overrides any file. With an explicit ``path`` only that
    file is read; otherwise two locations are tried, earlier ones winning:

    1. ``<user config dir>/.env`` (e.g. ``~/.config/mak/.env``) — where an
       installed MAK's ``/apikey`` setup stores keys.
    2. ``mak/.env`` next to this module — the legacy source-checkout location,
       **deprecated**: it sits inside the package directory and nothing enforces
       its mode, so a working copy is routinely left world-readable with live
       keys in it. Using it warns and names the replacement; the next release
       stops reading it.
    """
    if path is not None:
        _load_one_env_file(path)
        return
    _load_one_env_file(user_config_dir() / ".env")
    legacy = _legacy_env_path()
    if _load_one_env_file(legacy):
        print(
            f"mak: warning: read API keys from the legacy {legacy} (inside the "
            f"package directory). Move them to {user_config_dir() / '.env'} — "
            "MAK writes there with owner-only permissions. The legacy location "
            "is removed in the next release.",
            file=sys.stderr,
        )


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


def planner_endpoint(config: MakConfig) -> ResolvedEndpoint | None:
    """Return the planner's resolved endpoint, or None when it names none.

    Resolved against the merged endpoint set, so a planner may route through an
    endpoint the user saved interactively and never wrote into this file.
    """
    if not config.planner.endpoint:
        return None
    user_endpoints, _diagnostic = load_user_endpoints()
    merged = merge_endpoints(config.endpoints, user_endpoints)
    resolved = resolve_endpoints(merged)
    return require_endpoint(resolved, config.planner.endpoint, where="planner")


def _planner_api_key(config: MakConfig) -> str | None:
    """Resolve the planner's API key: endpoint, explicit env var, then inference.

    An endpoint is authoritative when one is named: it stated which variable
    holds its credential, so there is nothing to infer, and inferring anyway
    would let one service's key reach another's host.

    ``planner.api_key_env`` wins next — it is the only way to name the token
    for a protected gateway (``vllm --api-key``), whose model id tells us
    nothing. Falling through to ``None`` is deliberate rather than an oversight:
    a local planner has no key, and ``None`` is what lets the adapter apply its
    placeholder rule instead of forwarding a real cloud key to a local host.
    """
    endpoint = planner_endpoint(config)
    if endpoint is not None:
        return endpoint.api_key
    if config.planner.api_key_env:
        return os.environ.get(config.planner.api_key_env)
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


def _planner_backend(
    config: MakConfig, route: ResolvedEndpoint | None
) -> str | None:
    """Return the planner backend name for the configured route."""
    if route is None:
        return config.planner.backend
    # Every endpoint-backed planner speaks one of the transports
    # ``build_planner_llm`` already knows; the OpenAI-compatible client is the
    # one that takes an arbitrary base URL.
    return _TRANSPORT_TO_PLANNER_BACKEND.get(route.transport.value, "openai")


# Transport -> the planner backend that speaks it. Only the four API transports
# appear; a CLI wrapper never drives the planner.
_TRANSPORT_TO_PLANNER_BACKEND: dict[str, str] = {
    "openai_chat": "openai",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "ollama_native": "ollama",
}


def build_session(
    args: argparse.Namespace,
    config: MakConfig,
    sandbox: SandboxConfig | None = None,
) -> Session:
    """Assemble a ``Session`` and all its collaborators from configuration."""
    work_dir = Path(config.session.work_dir)
    mak_dir = Path(config.session.mak_dir)

    node_store = NodeStore(
        mak_dir / "node_store",
        version_retention=config.node_store.version_retention,
    )
    lock_table = LockTable(
        persist_path=mak_dir / "lock_table.json",
        default_timeout=config.session.lock_timeout_s,
    )
    # Resolve once and pass the roster down, so the registry, the pool caps and
    # the preflight cannot disagree about what is configured.
    roster = resolved_agents(config)
    # The endpoint's own published capability data, read once here rather than
    # inside an adapter factory. It lets an agent skip a reply format its model
    # has already said it cannot serve, instead of learning that by being
    # refused once per task.
    registry = build_registry(
        config,
        sandbox=sandbox,
        agents=roster,
        reported=ReportedCapabilities.load(),
    )
    # Health preflight: verify each configured agent is usable *before* dispatch,
    # so a missing CLI binary or absent API key surfaces now instead of as a
    # mid-run failure or a long timeout. The healthy set becomes the distribution
    # pool; the default agent must be among it.
    default_id = args.agent or default_agent_id(config)
    configured = [a.id for a in roster]
    healthy, unhealthy, why = healthy_agent_ids(registry, configured)
    for agent_id in unhealthy:
        # The adapter's own reason when it has one — "Ollama is not running at
        # http://localhost:11434", "model 'qwen2.5-coder:14b' is not pulled" —
        # because the generic line sends a local user to fix the wrong thing.
        reason = why.get(agent_id) or "missing API key/SDK, or CLI not on PATH"
        print(
            f"mak: warning: agent '{agent_id}' failed its health check "
            f"({reason}) — it will not be used.",
            file=sys.stderr,
        )
    if default_id not in healthy:
        raise ConfigError(
            f"the default agent '{default_id}' is not usable "
            "(failed its health check); configure a working agent/key"
        )
    # Per-agent config knobs reach the runner here: the read timeout is the
    # largest configured agent timeout (so no agent is cut short), and each
    # agent's max_instances caps its retained idle subprocess pool — keyed by
    # id, so two agents sharing a transport get their own caps.
    agent_runner = AgentRunner(
        timeout_s=max((a.timeout for a in roster), default=300),
        pool_caps={a.id: a.max_instances for a in roster},
        work_dir=str(work_dir),
    )
    route = planner_endpoint(config)
    planner = Planner(
        build_planner_llm(
            config.planner.model,
            # A named endpoint supplies the transport and the address; the
            # legacy backend/base_url pair is only consulted when there is none.
            backend=_planner_backend(config, route),
            base_url=route.base_url if route is not None else config.planner.base_url,
            api_key=_planner_api_key(config),
        ),
        max_retries=config.planner.max_retries,
        agent_types=healthy,
        agent_labels=[a.label() for a in roster if a.id in set(healthy)],
        strategy=config.planner.strategy,
        self_critique=config.planner.self_critique,
    )
    git_helper = (
        GitHelper(work_dir, commit_prefix=config.git.commit_prefix)
        if config.git.auto_commit
        else None
    )
    logger = SessionLogger(mak_dir / "session.log")
    session_id = f"mak-{int(time.time())}"

    return Session(
        session_id=session_id,
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
        default_agent_type=default_id,
        agent_pool=healthy,
        # One owner per project. Taken in initialize()/recover(), before anything
        # reads or mutates .mak/, and released by close().
        project_lease=ProjectLease(mak_dir, session_id),
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

    orphan: Path | None = None
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
        # The three ways a cascade stops without finishing. Each used to be
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
