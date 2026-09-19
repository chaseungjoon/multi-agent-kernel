"""Wave 20 plan validation: relaxed edges, declared API edges, structure, tables."""

from __future__ import annotations

from mak.core.types import NodeId, SubTask
from mak.planner.depgraph import build_dep_graph
from mak.planner.validation import PlanSemantics, validate_plan

LOAD = NodeId("m.py::function::load")
SHOW = NodeId("m.py::function::show")
SOURCES = {
    LOAD: "def load(uid):\n    return 1\n",
    SHOW: "def show(uid):\n    return load(uid)\n",
}


def _t(task_id: str, targets: list[NodeId], **kw: object) -> SubTask:
    return SubTask(task_id, kw.pop("description", f"task {task_id}"),  # type: ignore[arg-type]
                   list(targets), **kw)  # type: ignore[arg-type]


def _validate(
    plan: list[SubTask], semantic: PlanSemantics | None
) -> dict[str, SubTask]:
    graph = build_dep_graph(SOURCES)
    result = validate_plan(plan, graph, list(SOURCES), semantic=semantic)
    return {t.task_id: t for t in result.plan}


def _kinds(plan: list[SubTask], semantic: PlanSemantics | None) -> list[str]:
    graph = build_dep_graph(SOURCES)
    result = validate_plan(plan, graph, list(SOURCES), semantic=semantic)
    return [f.kind for f in result.findings]


class TestRelaxedEdges:
    def test_body_only_writer_no_longer_orders_its_callers(self) -> None:
        plan = [_t("w", [LOAD], changes_api=False), _t("c", [SHOW])]
        on = PlanSemantics(api_locks=True)
        assert _validate(plan, on)["c"].depends_on == []
        assert "relaxed_dep" in _kinds(plan, on)

    def test_without_api_locks_the_edge_stays(self) -> None:
        plan = [_t("w", [LOAD], changes_api=False), _t("c", [SHOW])]
        assert _validate(plan, PlanSemantics(api_locks=False))["c"].depends_on == ["w"]
        assert _validate(plan, None)["c"].depends_on == ["w"]

    def test_undeclared_and_api_changing_writers_keep_the_edge(self) -> None:
        on = PlanSemantics(api_locks=True)
        for declared in (None, True):
            plan = [_t("w", [LOAD], changes_api=declared), _t("c", [SHOW])]
            assert _validate(plan, on)["c"].depends_on == ["w"]

    def test_api_change_narrowed_elsewhere_relaxes(self) -> None:
        other = NodeId("n.py::function::x")
        plan = [
            _t("w", [LOAD, other], changes_api=True, api_targets=[other]),
            _t("c", [SHOW]),
        ]
        assert _validate(plan, PlanSemantics(api_locks=True))["c"].depends_on == []


class TestDeclaredApiEdges:
    def test_a_new_caller_named_in_the_description_is_ordered(self) -> None:
        new = NodeId("r.py::function::report")
        plan = [
            _t("w", [LOAD], changes_api=True),
            _t("n", [new], description="write report() that calls load for each id"),
        ]
        validated = _validate(plan, PlanSemantics(api_locks=True))
        assert validated["n"].depends_on == ["w"]
        assert "declared_api_dep" in _kinds(plan, PlanSemantics(api_locks=True))

    def test_a_contract_naming_it_is_ordered_too(self) -> None:
        new = NodeId("r.py::function::report")
        plan = [
            _t("w", [LOAD], changes_api=True),
            _t("n", [new], contract={new: "def report(load: int) -> str"}),
        ]
        assert _validate(plan, PlanSemantics())["n"].depends_on == ["w"]

    def test_undeclared_writers_add_no_description_edges(self) -> None:
        new = NodeId("r.py::function::report")
        plan = [_t("w", [LOAD]), _t("n", [new], description="calls load")]
        assert _validate(plan, PlanSemantics())["n"].depends_on == []


class TestStructureAndTables:
    def test_structure_writer_goes_first(self) -> None:
        init = NodeId("c.py::method::Order.__init__")
        total = NodeId("c.py::method::Order.total")
        plan = [_t("m", [total]), _t("s", [init])]
        validated = _validate(plan, PlanSemantics())
        assert validated["m"].depends_on == ["s"]
        assert "shared_structure" in _kinds(plan, PlanSemantics())

    def test_ordered_table_is_flagged_for_every_writer(self) -> None:
        chain = NodeId("mw.py::function::_install")
        plan = [_t("a", [chain]), _t("b", [chain], depends_on=["a"])]
        kinds = _kinds(plan, PlanSemantics(registrar_kinds={chain: "ordered"}))
        assert kinds.count("ordered_table") == 2

    def test_keyed_tables_are_not_flagged(self) -> None:
        table = NodeId("r.py::function::_register_all")
        plan = [_t("a", [table]), _t("b", [table])]
        kinds = _kinds(plan, PlanSemantics(registrar_kinds={table: "keyed"}))
        assert "ordered_table" not in kinds

    def test_same_declared_key_is_ordered_and_flagged(self) -> None:
        table = NodeId("r.py::function::_register_all")
        plan = [
            _t("a", [table], registry_keys={table: ["/users"]}),
            _t("b", [table], registry_keys={table: ["/users", "/x"]}),
        ]
        validated = _validate(plan, PlanSemantics())
        assert validated["b"].depends_on == ["a"]
        assert "registry_key_collision" in _kinds(plan, PlanSemantics())
