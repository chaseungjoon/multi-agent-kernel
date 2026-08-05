"""Tests for deterministic plan validation (Wave 10, Step 2)."""

from __future__ import annotations

from mak.core.types import NodeId, SubTask
from mak.planner.depgraph import build_dep_graph
from mak.planner.validation import PlanFinding, validate_plan


def _task(task_id: str, targets: list[str], **kw: object) -> SubTask:
    return SubTask(
        task_id=task_id,
        description=task_id,
        target_nodes=[NodeId(t) for t in targets],
        context_nodes=[NodeId(c) for c in kw.get("context", [])],  # type: ignore[arg-type]
        depends_on=list(kw.get("depends_on", [])),  # type: ignore[arg-type]
    )


def _kinds(findings: list[PlanFinding]) -> set[str]:
    return {f.kind for f in findings}


# A tiny two-file project: app.load calls util.parse_config.
_SOURCES = {
    "util.py::module_header::__header__": "",
    "util.py::function::parse_config": "def parse_config(s):\n    return s\n",
    "app.py::module_header::__header__": "from util import parse_config\n",
    "app.py::function::load": "def load():\n    return parse_config('x')\n",
}
_GRAPH = build_dep_graph({NodeId(k): v for k, v in _SOURCES.items()})
_INVENTORY = [NodeId(k) for k in _SOURCES]


class TestMissingEdges:
    def test_missing_edge_added(self) -> None:
        plan = [
            _task("edit-parse", ["util.py::function::parse_config"]),
            _task("edit-load", ["app.py::function::load"]),
        ]
        result = validate_plan(plan, _GRAPH, _INVENTORY)
        load = next(t for t in result.plan if t.task_id == "edit-load")
        assert load.depends_on == ["edit-parse"]
        assert "missing_dep" in _kinds(result.findings)

    def test_transitive_dep_not_re_added(self) -> None:
        # edit-load already depends on edit-parse -> no duplicate finding/edge.
        plan = [
            _task("edit-parse", ["util.py::function::parse_config"]),
            _task("edit-load", ["app.py::function::load"], depends_on=["edit-parse"]),
        ]
        result = validate_plan(plan, _GRAPH, _INVENTORY)
        load = next(t for t in result.plan if t.task_id == "edit-load")
        assert load.depends_on == ["edit-parse"]
        assert not [f for f in result.findings if f.kind == "missing_dep"]

    def test_mutual_reference_flagged_not_cycled(self) -> None:
        sources = {
            "m.py::module_header::__header__": "",
            "m.py::function::a": "def a():\n    return b()\n",
            "m.py::function::b": "def b():\n    return a()\n",
        }
        graph = build_dep_graph({NodeId(k): v for k, v in sources.items()})
        inv = [NodeId(k) for k in sources]
        plan = [
            _task("ta", ["m.py::function::a"]),
            _task("tb", ["m.py::function::b"]),
        ]
        result = validate_plan(plan, graph, inv)
        # One edge is added, the reverse is flagged mutual — never both (no cycle).
        edges = {(f.task_id) for f in result.findings if "added:" in f.message}
        deps = {t.task_id: t.depends_on for t in result.plan}
        assert not (deps["ta"] and deps["tb"])  # not a 2-cycle
        assert any("mutual" in f.message for f in result.findings)
        assert edges  # at least one real edge added


class TestSpurious:
    def test_spurious_flagged_not_removed(self) -> None:
        # Two unrelated tasks with a declared (baseless) dependency.
        sources = {
            "m.py::function::a": "def a():\n    return 1\n",
            "m.py::function::b": "def b():\n    return 2\n",
        }
        graph = build_dep_graph({NodeId(k): v for k, v in sources.items()})
        inv = [NodeId(k) for k in sources]
        plan = [
            _task("ta", ["m.py::function::a"]),
            _task("tb", ["m.py::function::b"], depends_on=["ta"]),
        ]
        result = validate_plan(plan, graph, inv)
        tb = next(t for t in result.plan if t.task_id == "tb")
        assert tb.depends_on == ["ta"]  # kept
        assert "spurious_dep" in _kinds(result.findings)


class TestGrounding:
    def test_wrong_kind_correction(self) -> None:
        inv = [NodeId("m.py::method::Cart.total")]
        graph = build_dep_graph({})
        plan = [_task("t", ["m.py::function::total"])]  # wrong kind (should be method)
        result = validate_plan(plan, graph, inv)
        assert result.plan[0].target_nodes == [NodeId("m.py::method::Cart.total")]
        assert "corrected_node" in _kinds(result.findings)

    def test_case_underscore_correction(self) -> None:
        inv = [NodeId("m.py::function::foo_bar")]
        plan = [_task("t", ["m.py::function::fooBar"])]
        result = validate_plan(plan, build_dep_graph({}), inv)
        assert result.plan[0].target_nodes == [NodeId("m.py::function::foo_bar")]

    def test_missing_class_prefix_correction(self) -> None:
        inv = [NodeId("m.py::method::Cart.total")]
        plan = [_task("t", ["m.py::method::total"])]  # missing 'Cart.' prefix
        result = validate_plan(plan, build_dep_graph({}), inv)
        assert result.plan[0].target_nodes == [NodeId("m.py::method::Cart.total")]

    def test_ambiguous_suggestions_only(self) -> None:
        inv = [
            NodeId("m.py::method::Cart.total"),
            NodeId("m.py::method::Order.total"),
        ]
        plan = [_task("t", ["m.py::method::total"])]  # two 'total' methods -> ambiguous
        result = validate_plan(plan, build_dep_graph({}), inv)
        # unchanged (ambiguous -> suggestions only)
        assert result.plan[0].target_nodes == [NodeId("m.py::method::total")]
        finding = next(f for f in result.findings if f.kind == "unknown_node")
        assert len(finding.suggestions) == 2

    def test_genuinely_new_node_silent(self) -> None:
        inv = [NodeId("m.py::function::existing")]
        plan = [_task("t", ["newfile.py::function::brand_new"])]
        result = validate_plan(plan, build_dep_graph({}), inv)
        new_id = NodeId("newfile.py::function::brand_new")
        assert result.plan[0].target_nodes == [new_id]
        assert result.findings == []

    def test_unknown_context_node_dropped(self) -> None:
        inv = [NodeId("m.py::function::real")]
        plan = [_task("t", ["m.py::function::real"], context=["ghost.py::function::x"])]
        result = validate_plan(plan, build_dep_graph({}), inv)
        assert result.plan[0].context_nodes == []
        assert "context_dropped" in _kinds(result.findings)


class TestGuardAndEdges:
    def test_whole_file_owner_guard_reverts(self) -> None:
        # Two tasks; a hallucinated 'thing.py' fuzzy-matches the existing whole-file
        # 'things.py' already owned by t1 -> correcting t2 would duplicate the owner.
        inv = [NodeId("things.py")]
        plan = [
            _task("t1", ["things.py"]),
            _task("t2", ["thing.py"]),
        ]
        result = validate_plan(plan, build_dep_graph({}), inv)
        t2 = next(t for t in result.plan if t.task_id == "t2")
        assert t2.target_nodes == [NodeId("thing.py")]  # reverted, not corrected
        assert "unknown_node" in _kinds(result.findings)
        assert "corrected_node" not in _kinds(result.findings)


class TestNoOps:
    def test_empty_plan(self) -> None:
        result = validate_plan([], _GRAPH, _INVENTORY)
        assert result.plan == []
        assert result.findings == []

    def test_empty_graph_and_inventory(self) -> None:
        plan = [_task("t", ["m.py::function::f"])]
        result = validate_plan(plan, build_dep_graph({}), [])
        # No inventory -> the target is treated as a new node (silent), no edges.
        assert result.plan[0].target_nodes == [NodeId("m.py::function::f")]
        assert result.findings == []


class TestForwardContext:
    """Context a sibling task will create survives, and orders the reader."""

    def test_context_targeted_by_another_task_is_kept(self) -> None:
        # 'home.py' does not exist yet — 'build' creates it. Dropping it left the
        # reader with an empty bundle and nothing to write its tests against.
        plan = [
            _task("build", ["home.py"]),
            _task("test", ["test_home.py"], context=["home.py"]),
        ]
        result = validate_plan(plan, _GRAPH, _INVENTORY)
        reader = next(t for t in result.plan if t.task_id == "test")
        assert reader.context_nodes == [NodeId("home.py")]
        assert "context_dropped" not in _kinds(result.findings)

    def test_genuinely_phantom_context_is_still_dropped(self) -> None:
        plan = [
            _task("build", ["home.py"]),
            _task("test", ["test_home.py"], context=["nowhere.py"]),
        ]
        result = validate_plan(plan, _GRAPH, _INVENTORY)
        reader = next(t for t in result.plan if t.task_id == "test")
        assert reader.context_nodes == []
        assert "context_dropped" in _kinds(result.findings)

    def test_forward_reference_adds_the_ordering_edge(self) -> None:
        plan = [
            _task("build", ["home.py"]),
            _task("test", ["test_home.py"], context=["home.py"]),
        ]
        result = validate_plan(plan, _GRAPH, _INVENTORY)
        reader = next(t for t in result.plan if t.task_id == "test")
        assert reader.depends_on == ["build"]
        assert any(
            "does not exist yet" in f.message for f in result.findings
        )

    def test_existing_context_needs_no_edge(self) -> None:
        # parse_config is committed already, so the reader can run at any time.
        plan = [
            _task("edit-parse", ["util.py::function::parse_config"]),
            _task("doc", ["docs.py"], context=["util.py::function::parse_config"]),
        ]
        result = validate_plan(plan, _GRAPH, _INVENTORY)
        doc = next(t for t in result.plan if t.task_id == "doc")
        assert doc.depends_on == []

    def test_mutual_forward_reference_is_flagged_not_cycled(self) -> None:
        plan = [
            _task("build", ["home.py"], depends_on=["test"]),
            _task("test", ["test_home.py"], context=["home.py"]),
        ]
        result = validate_plan(plan, _GRAPH, _INVENTORY)
        reader = next(t for t in result.plan if t.task_id == "test")
        assert reader.depends_on == []  # no cycle introduced
        assert any("order them manually" in f.message for f in result.findings)

    def test_context_is_not_corrected_toward_a_planned_id(self) -> None:
        # A near-miss of a *planned* target must not be auto-corrected: the
        # correction would name a node that does not exist and no one declared.
        plan = [
            _task("build", ["homescreen.py"]),
            _task("test", ["test_home.py"], context=["homescren.py"]),
        ]
        result = validate_plan(plan, _GRAPH, _INVENTORY)
        reader = next(t for t in result.plan if t.task_id == "test")
        assert reader.context_nodes == []
        assert "corrected_node" not in _kinds(result.findings)
