"""Wave 7: the kernel supplies callers (mak.planner.callers) and unseen targets."""

from __future__ import annotations

import pytest

from mak.core.types import NodeId, SubTask
from mak.planner.callers import (
    caller_findings,
    drop_tasks,
    find_missing_callers,
    propose_caller_tasks,
)
from mak.planner.depgraph import DepGraph, build_dep_graph
from mak.planner.validation import PlanSemantics, validate_plan

LIB = {
    "lib.py::module_header::__header__": "",
    "lib.py::function::target": "def target(a):\n    return a\n",
    "lib.py::function::other": "def other(a):\n    return a\n",
    "lib.py::class::Shape": "class Shape:\n    size = 1\n",
}


def _caller_file(path: str, *bodies: str, imports: str = "target") -> dict[str, str]:
    stem = path.removesuffix(".py")
    nodes = {f"{path}::module_header::__header__": f"from lib import {imports}\n"}
    for index, body in enumerate(bodies):
        name = f"{stem}{index + 1}"
        nodes[f"{path}::function::{name}"] = f"def {name}():\n    return {body}\n"
    return nodes


def _graph(*parts: dict[str, str]) -> tuple[DepGraph, list[NodeId]]:
    sources: dict[NodeId, str] = {}
    for part in (LIB, *parts):
        sources.update({NodeId(k): v for k, v in part.items()})
    return build_dep_graph(sources), list(sources)


def _task(
    task_id: str, *targets: str, changes_api: bool | None = None,
    contract: dict[str, str] | None = None, deps: list[str] | None = None,
) -> SubTask:
    return SubTask(
        task_id=task_id, description=f"task {task_id}",
        target_nodes=[NodeId(t) for t in targets], depends_on=deps or [],
        changes_api=changes_api,
        contract={NodeId(k): v for k, v in (contract or {}).items()},
    )


def _kinds(findings: list[object]) -> list[str]:
    return [f.kind for f in findings]  # type: ignore[attr-defined]


class TestFindingMissingCallers:
    def test_uncovered_callers_are_findings_and_one_task_per_file(self) -> None:
        graph, _ = _graph(
            _caller_file("a.py", "target(1)"),
            _caller_file("b.py", "target(2)", "target(3)"),
        )
        plan = [
            _task("change", "lib.py::function::target", changes_api=True,
                  contract={"lib.py::function::target": "def target(a, b=0)"}),
            _task("fix-a", "a.py::function::a1", changes_api=False),
        ]
        findings = caller_findings(plan, graph)
        assert _kinds(findings) == ["missing_caller", "missing_caller"]
        assert {f.suggestions[0] for f in findings} == {
            "b.py::function::b1", "b.py::function::b2",
        }
        proposal = propose_caller_tasks(plan, graph, max_tasks=25)
        assert proposal.proposed_ids == frozenset({"mak.callers.1"})
        added = proposal.plan[-1]
        assert added.target_nodes == [
            NodeId("b.py::function::b1"), NodeId("b.py::function::b2"),
        ]
        assert added.depends_on == ["change"]
        assert added.changes_api is False
        assert added.context_nodes == [NodeId("lib.py::function::target")]
        assert added.agent_type == ""
        assert "`def target(a, b=0)`" in added.description
        assert "Change only call sites" in added.description

    def test_findings_are_reworded_once_a_task_covers_them(self) -> None:
        graph, _ = _graph(_caller_file("b.py", "target(2)"))
        plan = [_task("change", "lib.py::function::target", changes_api=True)]
        findings = caller_findings(plan, graph)
        proposal = propose_caller_tasks(plan, graph, max_tasks=25)
        (reworded,) = proposal.annotate(findings)
        assert reworded.kind == "missing_caller"
        assert reworded.message.startswith("added: 'mak.callers.1' updates")
        assert "committed by `change`" in proposal.plan[-1].description

    def test_a_caller_shared_by_two_changes_gets_one_task_after_both(self) -> None:
        graph, _ = _graph(
            _caller_file("b.py", "target(1) + other(2)", imports="target, other")
        )
        plan = [
            _task("t1", "lib.py::function::target", changes_api=True),
            _task("t2", "lib.py::function::other", changes_api=True),
        ]
        proposal = propose_caller_tasks(plan, graph, max_tasks=25)
        (added,) = [t for t in proposal.plan if t.task_id in proposal.proposed_ids]
        assert added.target_nodes == [NodeId("b.py::function::b1")]
        assert added.depends_on == ["t1", "t2"]

    def test_an_undeclared_change_is_one_finding_and_no_task(self) -> None:
        graph, _ = _graph(_caller_file("b.py", "target(2)", "target(3)"))
        plan = [_task("t", "lib.py::function::target")]
        findings = caller_findings(plan, graph)
        assert _kinds(findings) == ["undeclared_api_callers"]
        assert findings[0].message.startswith("2 graph callers of these targets")
        assert propose_caller_tasks(plan, graph, max_tasks=25).proposed_ids == set()

    def test_a_body_only_change_says_nothing(self) -> None:
        graph, _ = _graph(_caller_file("b.py", "target(2)"))
        plan = [_task("t", "lib.py::function::target", changes_api=False)]
        assert caller_findings(plan, graph) == []

    def test_a_class_target_gets_findings_only(self) -> None:
        graph, _ = _graph(_caller_file("b.py", "Shape()", imports="Shape"))
        plan = [_task("t", "lib.py::class::Shape", changes_api=True)]
        assert _kinds(caller_findings(plan, graph)) == ["missing_caller"]
        assert propose_caller_tasks(plan, graph, max_tasks=25).proposed_ids == set()

    def test_a_whole_file_target_covers_its_callers(self) -> None:
        graph, _ = _graph(_caller_file("b.py", "target(2)"))
        plan = [
            _task("t", "lib.py::function::target", changes_api=True),
            _task("rewrite-b", "b.py"),
        ]
        assert find_missing_callers(plan, graph) == []
        assert propose_caller_tasks(plan, graph, max_tasks=25).proposed_ids == set()

    def test_a_task_that_would_break_an_invariant_is_refused(self) -> None:
        # c.py is held as one whole-file node, while another task writes one of
        # its symbols: a whole-file caller task would mix granularities.
        graph, _ = _graph(
            {"c.py": "from lib import target\n\n\ndef c1():\n    return target(1)\n"}
        )
        plan = [
            _task("t", "lib.py::function::target", changes_api=True),
            _task("helper", "c.py::function::helper"),
        ]
        proposal = propose_caller_tasks(plan, graph, max_tasks=25)
        assert proposal.proposed_ids == set()
        (refused,) = proposal.findings
        assert refused.kind == "caller_task_refused"
        assert "one granularity per file" in refused.message

    def test_the_cap_is_respected_and_reported(self) -> None:
        graph, _ = _graph(
            _caller_file("a.py", "target(1)"),
            _caller_file("b.py", "target(2)"),
            _caller_file("c.py", "target(3)"),
        )
        plan = [_task("t", "lib.py::function::target", changes_api=True)]
        proposal = propose_caller_tasks(plan, graph, max_tasks=1)
        assert proposal.proposed_ids == frozenset({"mak.callers.1"})
        assert proposal.plan[-1].target_nodes == [NodeId("a.py::function::a1")]
        (capped,) = proposal.findings
        assert capped.kind == "caller_tasks_capped"
        assert capped.message.startswith("2 more caller files got no task")

    def test_ids_never_clash_with_the_planners(self) -> None:
        graph, _ = _graph(_caller_file("b.py", "target(2)"))
        plan = [
            _task("mak.callers.1", "lib.py::function::target", changes_api=True),
        ]
        proposal = propose_caller_tasks(plan, graph, max_tasks=25)
        assert proposal.proposed_ids == frozenset({"mak.callers.1_2"})

    def test_validation_reports_but_never_adds(self) -> None:
        graph, inventory = _graph(_caller_file("b.py", "target(2)"))
        plan = [_task("t", "lib.py::function::target", changes_api=True)]
        result = validate_plan(plan, graph, inventory, semantic=PlanSemantics())
        assert [t.task_id for t in result.plan] == ["t"]
        assert "missing_caller" in _kinds(result.findings)
        # Without semantic validation the check does not run.
        plain = validate_plan(plan, graph, inventory)
        assert "missing_caller" not in _kinds(plain.findings)


class TestDropTasks:
    def test_dropping_proposed_tasks_removes_edges_into_them(self) -> None:
        plan = [
            _task("t", "lib.py::function::target"),
            _task("mak.callers.1", "b.py::function::b1", deps=["t"]),
            _task("later", "c.py::function::c1", deps=["t", "mak.callers.1"]),
        ]
        kept = drop_tasks(plan, frozenset({"mak.callers.1"}))
        assert [t.task_id for t in kept] == ["t", "later"]
        assert kept[1].depends_on == ["t"]


class TestUnseenTargets:
    """A retrieval plan's new id in a file it never saw is never silently new."""

    def _validate(self, target: str, seen: frozenset[str] | None) -> list[object]:
        graph, inventory = _graph(_caller_file("b.py", "target(2)"))
        plan = [_task("t", target, changes_api=False)]
        result = validate_plan(
            plan, graph, inventory, semantic=PlanSemantics(seen_files=seen)
        )
        return result.findings  # type: ignore[return-value]

    def test_a_new_id_in_an_unseen_file_is_flagged(self) -> None:
        findings = self._validate("b.py::function::brand_new", frozenset())
        assert _kinds(findings) == ["unseen_target"]
        assert "b.py::function::b1" in findings[0].suggestions  # type: ignore[attr-defined]

    def test_the_same_id_is_silent_when_the_file_was_seen(self) -> None:
        assert self._validate("b.py::function::brand_new", frozenset({"b.py"})) == []

    def test_the_same_id_is_silent_without_retrieval(self) -> None:
        assert self._validate("b.py::function::brand_new", None) == []

    def test_an_exact_existing_id_in_an_unseen_file_is_accepted(self) -> None:
        assert self._validate("b.py::function::b1", frozenset()) == []

    def test_one_confident_match_is_still_corrected(self) -> None:
        findings = self._validate("b.py::method::b1", frozenset())
        assert _kinds(findings) == ["corrected_node"]

    @pytest.mark.parametrize("target", ["new_file.py", "new_file.py::function::f"])
    def test_a_target_in_a_new_file_is_legitimate(self, target: str) -> None:
        assert self._validate(target, frozenset()) == []
