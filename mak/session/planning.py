"""Prepare a plan for installation: refuse, validate and assign agents."""

from __future__ import annotations

from dataclasses import replace

from mak.agent_runner.registry import AdapterRegistry
from mak.config import MakConfig
from mak.core.exceptions import SessionError
from mak.core.logging import EventType
from mak.core.paths import unsafe_node_id_reason
from mak.core.types import SubTask
from mak.node_store.store import NodeStore
from mak.planner.depgraph import DepGraph, dep_graph_from_store
from mak.planner.validation import PlanFinding, PlanSemantics, validate_plan
from mak.semantic.locking import registrar_kinds
from mak.session.events import EventLog
from mak.session.workspace import Workspace


class PlanPreparer:
    """Everything a plan goes through between the planner and the scheduler."""

    def __init__(
        self,
        *,
        config: MakConfig,
        store: NodeStore,
        workspace: Workspace,
        registry: AdapterRegistry,
        agent_pool: list[str] | None,
        default_agent_type: str | None,
        log: EventLog,
    ) -> None:
        self._config = config
        self._store = store
        self._workspace = workspace
        self._registry = registry
        # Healthy configured agent types to distribute unassigned tasks across
        # (round-robin). Falls back to [default_agent_type] when not provided.
        self._agent_pool = list(agent_pool) if agent_pool else None
        self._default_agent_type = default_agent_type
        self._log = log

    def validate(
        self, subtasks: list[SubTask], graph: DepGraph | None = None
    ) -> tuple[list[SubTask], list[PlanFinding]]:
        """Run deterministic plan validation, unless disabled in config."""
        if not self._config.planner.validate:
            return subtasks, []
        if graph is None:
            graph = dep_graph_from_store(self._store)
        targets = {node for task in subtasks for node in task.target_nodes}
        semantic = PlanSemantics(
            api_locks=self._config.semantic.api_locks,
            registrar_kinds={
                node: str(kind)
                for node, kind in registrar_kinds(self._store, targets).items()
            },
        )
        result = validate_plan(
            subtasks, graph, self._store.list_nodes(), semantic=semantic
        )
        return result.plan, result.findings

    def log_findings(self, findings: list[PlanFinding]) -> None:
        """Log one PLAN_VALIDATED event with a per-kind finding count."""
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.kind] = counts.get(finding.kind, 0) + 1
        self._log(EventType.PLAN_VALIDATED, counts=counts, total=len(findings))

    def reject_unsafe_targets(self, subtasks: list[SubTask]) -> None:
        """Refuse a plan whose targets would write outside the working directory.

        ``parse_plan`` applies the same rule to planner output, but this is the
        funnel every plan passes through — the interactive app installs a plan
        directly, and each cascade wave builds one from scratch. Neither touches
        the planner's parser, so without this check two of the three ways a plan
        reaches the scheduler are ungated.

        Raised rather than corrected: an escaping target is not a typo validation
        can ground, and silently rewriting one would hide what was asked for.
        """
        mak_name = self._workspace.mak_dir_name
        offenders = [
            (task.task_id, str(node), reason)
            for task in subtasks
            for node in task.target_nodes
            if (reason := unsafe_node_id_reason(str(node), mak_dir_name=mak_name))
            is not None
        ]
        if not offenders:
            return
        listed = "; ".join(f"{tid} -> {node} ({why})" for tid, node, why in offenders)
        raise SessionError(
            f"refusing to install a plan with {len(offenders)} target(s) outside "
            f"the working directory: {listed}"
        )

    def assign_agents(self, subtasks: list[SubTask]) -> list[SubTask]:
        """Assign a valid agent type to every task before dispatch.

        Three cases, so ``registry.get(agent_type)`` can never raise
        ``UnknownAgentTypeError`` mid-run and multi-provider rosters are actually
        used rather than everything landing on the first agent:

        - **empty** ``agent_type`` → distributed round-robin across the agent pool
          (the healthy configured agent types), so a plan that omits agent types
          spreads work across every provider instead of only the default;
        - **unconfigured/hallucinated** ``agent_type`` (planner named a type that
          is not registered) → remapped to the pool's first entry, with a warning,
          instead of crashing dispatch;
        - **valid** ``agent_type`` → left as-is.

        With no pool (e.g. a direct construction that sets every task's type
        explicitly), tasks are returned unchanged.
        """
        pool = self._agent_pool or (
            [self._default_agent_type] if self._default_agent_type else []
        )
        if not pool:
            return subtasks
        known = set(self._known_agent_types())
        out: list[SubTask] = []
        rr = 0
        for task in subtasks:
            agent_type = task.agent_type
            if not agent_type:
                agent_type = pool[rr % len(pool)]
                rr += 1
            elif known and agent_type not in known:
                self._log(
                    EventType.AGENT_REMAPPED,
                    task_id=task.task_id,
                    remapped_agent_type=agent_type,
                    to=pool[0],
                )
                agent_type = pool[0]
            out.append(
                task if agent_type == task.agent_type
                else replace(task, agent_type=agent_type)
            )
        return out

    def _known_agent_types(self) -> list[str]:
        """Agent types the registry can resolve (empty if it can't enumerate)."""
        lister = getattr(self._registry, "list_types", None)
        return list(lister()) if callable(lister) else []
