"""Assemble a :class:`~mak.session.Session` and its collaborators from a config."""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from pathlib import Path

from mak.agent_runner.runner import AgentRunner
from mak.agent_runner.sandbox import SandboxConfig
from mak.application.keys import planner_endpoint, resolve_planner_key
from mak.bootstrap import (
    build_registry,
    default_agent_id,
    healthy_agent_ids,
    resolved_agents,
)
from mak.config import MakConfig
from mak.core.exceptions import ConfigError
from mak.core.logging import SessionLogger
from mak.endpoints.resolution import ResolvedEndpoint
from mak.git_integration.git import GitHelper
from mak.lock_manager.lock_table import LockTable
from mak.lock_manager.project_lease import ProjectLease
from mak.models.registry import ReportedCapabilities
from mak.node_store.store import NodeStore
from mak.planner.llm import build_planner_llm
from mak.planner.planner import Planner
from mak.session import Session
from mak.test_runner import build_test_runner

# Transport -> the planner backend that speaks it. Only the four API transports
# appear; a CLI wrapper never drives the planner.
_TRANSPORT_TO_PLANNER_BACKEND: dict[str, str] = {
    "openai_chat": "openai",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "ollama_native": "ollama",
}


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


def _warn_unhealthy(unhealthy: list[str], why: Mapping[str, str | None]) -> None:
    """Print one stderr line per agent that failed its health check."""
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


def build_session(
    config: MakConfig,
    *,
    env: Mapping[str, str] | None = None,
    sandbox: SandboxConfig | None = None,
    default_agent: str | None = None,
) -> Session:
    """Assemble a ``Session`` and all its collaborators from configuration.

    ``env`` is where every credential is read from (default: the process
    environment). ``default_agent`` overrides the config's default agent id.
    The session is built, not initialized — call ``initialize()`` next.
    """
    work_dir = Path(config.session.work_dir)
    mak_dir = Path(config.session.mak_dir)
    # Resolve once and pass the roster down, so the registry, the pool caps and
    # the preflight cannot disagree about what is configured.
    roster = resolved_agents(config, env=env)
    # The endpoint's own published capability data, read once here rather than
    # inside an adapter factory. It lets an agent skip a reply format its model
    # has already said it cannot serve, instead of learning that by being
    # refused once per task.
    registry = build_registry(
        config, sandbox=sandbox, agents=roster, reported=ReportedCapabilities.load()
    )
    # Health preflight: verify each configured agent is usable *before* dispatch,
    # so a missing CLI binary or absent API key surfaces now instead of as a
    # mid-run failure or a long timeout. The healthy set becomes the distribution
    # pool; the default agent must be among it.
    default_id = default_agent or default_agent_id(config)
    healthy, unhealthy, why = healthy_agent_ids(registry, [a.id for a in roster])
    _warn_unhealthy(unhealthy, why)
    if default_id not in healthy:
        raise ConfigError(
            f"the default agent '{default_id}' is not usable "
            "(failed its health check); configure a working agent/key"
        )
    route = planner_endpoint(config, env=env)
    planner = Planner(
        build_planner_llm(
            config.planner.model,
            # A named endpoint supplies the transport and the address; the
            # legacy backend/base_url pair is only consulted when there is none.
            backend=_planner_backend(config, route),
            base_url=route.base_url if route is not None else config.planner.base_url,
            api_key=resolve_planner_key(config, env),
        ),
        max_retries=config.planner.max_retries,
        agent_types=healthy,
        agent_labels=[a.label() for a in roster if a.id in set(healthy)],
        strategy=config.planner.strategy,
        self_critique=config.planner.self_critique,
    )
    session_id = f"mak-{int(time.time())}"
    return Session(
        session_id=session_id,
        config=config,
        node_store=NodeStore(
            mak_dir / "node_store",
            version_retention=config.node_store.version_retention,
        ),
        lock_table=LockTable(
            persist_path=mak_dir / "lock_table.json",
            default_timeout=config.session.lock_timeout_s,
        ),
        registry=registry,
        # Per-agent config knobs reach the runner here: the read timeout is the
        # largest configured agent timeout (so no agent is cut short), and each
        # agent's max_instances caps its retained idle subprocess pool — keyed
        # by id, so two agents sharing a transport get their own caps.
        # AgentRunner satisfies the session's loose _Assigner protocol at
        # runtime; the nominal mismatch is the protocol's object-typed params.
        agent_runner=AgentRunner(  # type: ignore[arg-type]
            timeout_s=max((a.timeout for a in roster), default=300),
            pool_caps={a.id: a.max_instances for a in roster},
            work_dir=str(work_dir),
        ),
        planner=planner,
        git_helper=(
            GitHelper(work_dir, commit_prefix=config.git.commit_prefix)
            if config.git.auto_commit
            else None
        ),
        logger=SessionLogger(mak_dir / "session.log"),
        test_runner=build_test_runner(config.session.test_command, work_dir),
        default_agent_type=default_id,
        agent_pool=healthy,
        # One owner per project. Taken in initialize()/recover(), before anything
        # reads or mutates .mak/, and released by close().
        project_lease=ProjectLease(mak_dir, session_id),
    )
