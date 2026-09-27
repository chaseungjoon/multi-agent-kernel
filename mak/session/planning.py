"""Prepare a plan for installation: plan, refuse, validate and assign agents.

Everything a plan goes through between the user's request and the scheduler,
kept out of the session's state machine:

- **one index per store generation** — the dependency graph, its reverse index
  and the planner's inventory view are built once and reused until a commit
  moves ``NodeStore.generation`` (the pattern the cross-file symbol index
  uses). A planned wave used to parse the whole store twice, once to validate
  and once to install;
- **proposing** — the planner's call, logged live as ``PLANNER_CALL`` events;
  validation; and, on this path only, the caller-update tasks MAK adds for the
  signature changes the plan declares (:mod:`mak.planner.callers`);
- **carrying the planning summary** from the proposal to the wave it installs,
  so ``SessionResult.metrics`` reports what planning cost.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from mak.agent_runner.registry import AdapterRegistry
from mak.config import MakConfig
from mak.core.exceptions import SessionError
from mak.core.logging import EventType
from mak.core.paths import unsafe_node_id_reason
from mak.core.types import NodeId, SubTask
from mak.node_store.store import NodeStore
from mak.planner.callers import propose_caller_tasks
from mak.planner.depgraph import DepGraph, build_dep_graph, referrers, store_sources
from mak.planner.inventory import InventoryView
from mak.planner.telemetry import (
    NO_PLANNING,
    PlannerCall,
    PlannerObserver,
    PlanningSummary,
)
from mak.planner.validation import PlanFinding, PlanSemantics, validate_plan
from mak.semantic.locking import registrar_kinds
from mak.session.events import EventLog
from mak.session.types import PlanProposal
from mak.session.workspace import Workspace

if TYPE_CHECKING:
    from mak.planner.planner import Planner

# Findings the caller pass re-derives on the augmented plan.
_CALLER_KINDS = frozenset({"missing_caller", "undeclared_api_callers"})


@dataclass(frozen=True, slots=True)
class PlanningIndex:
    """What planning and validation derive from one store generation.

    ``DepGraph`` is treated as read-only: the wave that installs a plan keeps
    this graph as its *pre*-wave graph, and a later generation gets a new index
    rather than a mutated one.
    """

    generation: int
    graph: DepGraph
    referrers: dict[NodeId, frozenset[NodeId]]
    view: InventoryView
    build_ms: float


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
        self._index: PlanningIndex | None = None
        # Set by ``index()`` when it had to build: the first PLANNER_CALL of the
        # next plan reports the build time, and a reused index reports none.
        self._unreported_build_ms = 0.0
        self._summary: PlanningSummary = NO_PLANNING

    # -- the index ---------------------------------------------------------

    def index(self) -> PlanningIndex:
        """Return the index for the store's current generation, building it once."""
        generation = self._store.generation
        if self._index is None or self._index.generation != generation:
            started = time.perf_counter()
            sources = store_sources(self._store)
            graph = build_dep_graph(sources)
            reverse = referrers(graph)
            view = InventoryView(
                list(sources), sources=sources, graph=graph, referrers=reverse
            )
            build_ms = (time.perf_counter() - started) * 1000.0
            self._index = PlanningIndex(generation, graph, reverse, view, build_ms)
            self._unreported_build_ms = build_ms
        return self._index

    # -- proposing ---------------------------------------------------------

    def propose(self, planner: Planner, user_task: str) -> PlanProposal:
        """Plan ``user_task``, validate it, and add the caller tasks MAK can see.

        Caller tasks are proposed here and only here: ``install_plan``
        re-validates every plan, so a reviewer who drops a proposed task sees
        its callers reported again as ``missing_caller`` rather than silently
        re-added.
        """
        index = self.index()
        if hasattr(planner, "plan"):
            outcome = planner.plan(
                user_task, index.view.inventory, view=index.view,
                observer=self._observer(),
            )
            planned, seen, summary = outcome.plan, outcome.seen_files, outcome.summary
        else:  # a planner that only decomposes (a test double, a third party)
            planned = planner.decompose(user_task, index.view.inventory)
            seen, summary = None, NO_PLANNING
        subtasks, findings = self.validate(planned, seen_files=seen)
        proposed: frozenset[str] = frozenset()
        settings = self._config.planner
        if settings.validate and settings.auto_caller_tasks:
            subtasks, findings, proposed = self._add_caller_tasks(
                subtasks, findings, seen
            )
        self._summary = summary
        return PlanProposal(
            subtasks=subtasks,
            findings=findings,
            proposed_task_ids=proposed,
            planning=summary,
        )

    def _add_caller_tasks(
        self,
        subtasks: list[SubTask],
        findings: list[PlanFinding],
        seen: frozenset[str] | None,
    ) -> tuple[list[SubTask], list[PlanFinding], frozenset[str]]:
        """Propose caller tasks and re-validate the plan that carries them."""
        index = self.index()
        proposal = propose_caller_tasks(
            subtasks, index.graph, index.referrers,
            max_tasks=self._config.planner.max_caller_tasks,
        )
        first = proposal.annotate(findings)
        if not proposal.proposed_ids:
            return subtasks, [*first, *proposal.findings], frozenset()
        # The first pass's missing_caller findings explain the proposal and
        # stay; everything else new comes from validating the augmented plan.
        augmented, again = self.validate(proposal.plan, seen_files=seen)
        kept = [f for f in first if f.kind != "undeclared_api_callers"]
        known = set(kept)
        extra = [
            f for f in again
            if f.kind == "undeclared_api_callers"
            or (f.kind not in _CALLER_KINDS and f not in known)
        ]
        return augmented, [*kept, *proposal.findings, *extra], proposal.proposed_ids

    def _observer(self) -> PlannerObserver:
        """Log each planner call as a PLANNER_CALL event, the index build first."""
        build_ms = self._unreported_build_ms
        self._unreported_build_ms = 0.0
        first = True

        def observe(call: PlannerCall) -> None:
            nonlocal first
            payload: dict[str, object] = asdict(call)
            if first:
                payload["index_build_ms"] = round(build_ms, 3)
                first = False
            self._log(EventType.PLANNER_CALL, **payload)

        return observe

    def take_planning_summary(self) -> PlanningSummary:
        """Return the last proposal's planning summary once, then zeros.

        ``install_plan`` hands it to the wave it starts, so the planned wave
        reports what planning cost and a following cascade wave reports zeros.
        """
        summary, self._summary = self._summary, NO_PLANNING
        return summary

    # -- validation --------------------------------------------------------

    def validate(
        self,
        subtasks: list[SubTask],
        graph: DepGraph | None = None,
        *,
        seen_files: frozenset[str] | None = None,
    ) -> tuple[list[SubTask], list[PlanFinding]]:
        """Run deterministic plan validation, unless disabled in config."""
        if not self._config.planner.validate:
            return subtasks, []
        index = self.index()
        if graph is None:
            graph = index.graph
        targets = {node for task in subtasks for node in task.target_nodes}
        semantic = PlanSemantics(
            api_locks=self._config.semantic.api_locks,
            registrar_kinds={
                node: str(kind)
                for node, kind in registrar_kinds(self._store, targets).items()
            },
            seen_files=seen_files,
            referrers=index.referrers if graph is index.graph else None,
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
