"""Wave 7 on the session side: the planning index, proposals, telemetry, metrics."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import mak.session.planning as planning_module
from mak.config import MakConfig
from mak.core.exceptions import PlannerFailedError
from mak.core.logging import EventType, SessionLogger
from mak.core.types import NodeId
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.planner.depgraph import dep_graph_from_store
from mak.planner.planner import Planner
from mak.session import Session, SessionState
from tests.test_session import FakeRegistry, StagingRunner, _config, _task

_LIB = "def target(a):\n    return a\n\n\ndef load():\n    return 0\n"
_CALLER = "from lib import target\n\n\ndef use():\n    return target(1)\n"
_CHANGE = json.dumps([{
    "task_id": "change",
    "description": "add a parameter",
    "target_nodes": ["lib.py::function::target"],
    "changes_api": True,
    "contract": {"lib.py::function::target": "def target(a, b=0)"},
}])
_BODY_ONLY = json.dumps([{
    "task_id": "body", "description": "tweak load",
    "target_nodes": ["lib.py::function::load"], "changes_api": False,
}])


class _Billed:
    """A planner LLM double: scripted replies, per-call usage like a backend."""

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []
        self.last_usage: dict[str, int] = {}

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.last_usage = {"input_tokens": 120, "output_tokens": 30,
                           "cached_input_tokens": 50}
        return self._replies.pop(0)


def _session(
    tmp_path: Path,
    replies: Sequence[str],
    *,
    config: MakConfig | None = None,
    logger: SessionLogger | None = None,
) -> Session:
    (tmp_path / "lib.py").write_text(_LIB)
    (tmp_path / "caller.py").write_text(_CALLER)
    store = NodeStore(tmp_path / "store")
    planner = Planner(_Billed(replies), strategy="auto")
    planner._sleep = lambda _s: None  # type: ignore[method-assign]
    return Session(
        session_id="s1", config=config or _config(tmp_path), node_store=store,
        lock_table=LockTable(), registry=FakeRegistry(),  # type: ignore[arg-type]
        agent_runner=StagingRunner(store),  # type: ignore[arg-type]
        planner=planner, logger=logger,
    )


def _events(logger: SessionLogger, kind: EventType) -> list[dict[str, object]]:
    return [e.payload for e in logger.read_log() if e.event_type is kind]


class TestCallerProposals:
    def test_the_proposal_carries_mak_caller_tasks(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_CHANGE])
        session.initialize()
        proposal = session.propose_plan("add b to target")
        assert proposal.proposed_task_ids == frozenset({"mak.callers.1"})
        added = next(t for t in proposal.subtasks if t.task_id == "mak.callers.1")
        assert added.target_nodes == [NodeId("caller.py::function::use")]
        assert added.depends_on == ["change"]
        (missing,) = [f for f in proposal.findings if f.kind == "missing_caller"]
        assert missing.message.startswith("added: 'mak.callers.1' updates")

    def test_dropped_proposals_are_reported_not_re_added(self, tmp_path: Path) -> None:
        logger = SessionLogger(tmp_path / "session.log")
        session = _session(tmp_path, [_CHANGE], logger=logger)
        session.initialize()
        answers = iter(["d", "a"])
        printed: list[str] = []
        session.plan(
            "add b to target", prompt_fn=lambda _m: next(answers),
            printer=printed.append,
        )
        assert [t for t in session.wave.require_scheduler().dag.tasks] == ["change"]
        (missing,) = [f for f in session.last_plan_findings
                      if f.kind == "missing_caller"]
        assert missing.message.endswith("no task updates it")
        (validated,) = _events(logger, EventType.PLAN_VALIDATED)
        assert validated["counts"] == {"missing_caller": 1}  # type: ignore[comparison-overlap]
        assert any("[proposed by MAK]" in line for line in printed)

    def test_approving_installs_the_proposals(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_CHANGE])
        session.initialize()
        session.plan(
            "add b to target", prompt_fn=lambda _m: "a", printer=lambda _l: None
        )
        assert set(session.wave.require_scheduler().dag.tasks) == {
            "change", "mak.callers.1",
        }

    def test_auto_caller_tasks_off_leaves_findings_only(self, tmp_path: Path) -> None:
        base = _config(tmp_path)
        config = replace(base, planner=replace(base.planner, auto_caller_tasks=False))
        session = _session(tmp_path, [_CHANGE], config=config)
        session.initialize()
        proposal = session.propose_plan("add b to target")
        assert proposal.proposed_task_ids == frozenset()
        assert [t.task_id for t in proposal.subtasks] == ["change"]
        assert "missing_caller" in {f.kind for f in proposal.findings}


class TestTelemetry:
    def test_every_planner_call_is_logged_without_prompt_text(
        self, tmp_path: Path
    ) -> None:
        logger = SessionLogger(tmp_path / "session.log")
        session = _session(tmp_path, ["{broken", _BODY_ONLY], logger=logger)
        session.initialize()
        session.propose_plan("the secret request")
        calls = _events(logger, EventType.PLANNER_CALL)
        assert [c["outcome"] for c in calls] == ["rejected", "plan"]
        assert [c["strategy"] for c in calls] == ["full", "full"]
        assert "index_build_ms" in calls[0] and "index_build_ms" not in calls[1]
        assert calls[0]["input_tokens"] == 120
        assert "the secret request" not in (tmp_path / "session.log").read_text()
        assert "def target" not in (tmp_path / "session.log").read_text()

    def test_a_failed_plan_still_logs_what_it_paid_for(self, tmp_path: Path) -> None:
        logger = SessionLogger(tmp_path / "session.log")
        session = _session(tmp_path, ["{a", "{b", "{c"], logger=logger)
        session.initialize()
        with pytest.raises(PlannerFailedError):
            session.propose_plan("anything")
        calls = _events(logger, EventType.PLANNER_CALL)
        assert [c["attempt"] for c in calls] == [1, 2, 3]
        assert all(c["input_tokens"] == 120 for c in calls)


class TestPlanningMetrics:
    def test_the_planned_wave_reports_planning_and_a_cascade_wave_zeros(
        self, tmp_path: Path
    ) -> None:
        session = _session(tmp_path, [_BODY_ONLY])
        session.initialize()
        proposal = session.propose_plan("tweak load")
        assert proposal.planning.calls == 1
        session.install_plan(proposal.subtasks, objective="tweak load")
        first = session.run()
        assert first.metrics["planner_calls"] == 1.0
        assert first.metrics["planner_rounds"] == 1.0
        assert first.metrics["planner_input_tokens"] == 120.0
        assert first.metrics["planner_cached_tokens"] == 50.0
        assert first.metrics["planner_output_tokens"] == 30.0
        # A following wave the planner did not plan (a cascade) reports zeros.
        session.install_plan([_task("again", ["lib.py::function::load"])])
        second = session.run()
        assert second.state is SessionState.COMPLETED
        assert second.metrics["planner_calls"] == 0.0
        assert second.metrics["planner_input_tokens"] == 0.0


class TestPlanningIndex:
    def test_propose_and_install_build_the_graph_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        builds: list[int] = []
        real = planning_module.build_dep_graph

        def counting(sources: object) -> object:
            builds.append(1)
            return real(sources)  # type: ignore[arg-type]

        monkeypatch.setattr(planning_module, "build_dep_graph", counting)
        session = _session(tmp_path, [_BODY_ONLY])
        session.initialize()
        proposal = session.propose_plan("tweak load")
        session.install_plan(proposal.subtasks)
        assert len(builds) == 1

    def test_the_cached_graph_matches_a_fresh_build_after_a_commit(
        self, tmp_path: Path
    ) -> None:
        session = _session(tmp_path, [])
        session.initialize()
        preparer = session._parts.planning
        store = session._node_store
        before = preparer.index()
        assert preparer.index() is before
        store.sync_file(
            "caller.py", _CALLER + "\n\ndef more():\n    return target(2)\n"
        )
        after = preparer.index()
        assert after is not before and after.generation != before.generation
        fresh = dep_graph_from_store(store)
        assert after.graph.references == fresh.references
        assert after.graph.definers == fresh.definers
        assert after.referrers[NodeId("lib.py::function::target")] == frozenset({
            NodeId("caller.py::function::use"), NodeId("caller.py::function::more"),
        })

    def test_install_keeps_the_pre_wave_graph(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [])
        session.initialize()
        session.install_plan([_task("t", ["lib.py::function::load"])])
        assert session.wave.graph is session._parts.planning.index().graph
