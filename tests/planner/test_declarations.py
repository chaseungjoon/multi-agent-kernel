"""Wave 20 planner schema: interface declarations and contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mak.core.exceptions import ContractError
from mak.core.task_codec import subtask_from_dict, subtask_to_dict
from mak.core.types import NodeId, SubTask
from mak.planner.contracts import (
    contract_stub,
    implementation_mismatch,
    normalize_contract,
    parse_contract,
)
from mak.planner.planner import _plan_to_json, parse_plan
from mak.scheduler.dag import DAG
from mak.scheduler.scheduler import Scheduler


def _plan(**extra: object) -> str:
    task: dict[str, object] = {
        "task_id": "t",
        "description": "d",
        "target_nodes": ["m.py::function::f", "m.py::function::g"],
    }
    task.update(extra)
    return json.dumps([task])


class TestContracts:
    @pytest.mark.parametrize(
        "text",
        [
            "def f(a: int) -> str",
            "def f(a: int) -> str:",
            "def f(a: int) -> str: ...",
            "  def f(a: int) -> str  ",
        ],
    )
    def test_the_natural_spellings_normalize_alike(self, text: str) -> None:
        assert normalize_contract(text) == "def f(a: int) -> str"

    def test_async_and_class_contracts(self) -> None:
        assert parse_contract("async def f()").signature == "async def f()"
        contract = parse_contract("class Order(Base)")
        assert contract.is_class and contract.name == "Order"

    @pytest.mark.parametrize(
        "text", ["", "f(a)", "x = 1", "def f(a) -> int\ndef g()", "def f(:"]
    )
    def test_non_signatures_are_refused(self, text: str) -> None:
        with pytest.raises(ContractError):
            parse_contract(text)

    def test_stub_is_parseable_definition(self) -> None:
        assert contract_stub("def f(a: int) -> str") == "def f(a: int) -> str: ...\n"

    def test_matching_implementation(self) -> None:
        source = 'def f(a: int) -> str:\n    """Doc."""\n    return str(a)\n'
        assert implementation_mismatch("def f(a: int) -> str", source) is None

    def test_signature_drift_is_named(self) -> None:
        reason = implementation_mismatch(
            "def f(a: int) -> str", "def f(a: int, b: int) -> str:\n    pass\n"
        )
        assert reason is not None and "declared" in reason and "b: int" in reason

    def test_missing_definition_and_bad_source(self) -> None:
        assert "defines no 'f'" in str(
            implementation_mismatch("def f()", "def g():\n    pass\n")
        )
        assert "does not parse" in str(implementation_mismatch("def f()", "def ("))

    def test_method_contract_found_inside_a_class(self) -> None:
        source = "class C:\n    def get(self, k: str) -> int:\n        return 1\n"
        assert implementation_mismatch("def get(self, k: str) -> int", source) is None


class TestPlanDeclarations:
    def test_undeclared_plan_is_unchanged(self) -> None:
        (task,) = parse_plan(_plan())
        assert task.changes_api is None
        assert task.api_targets == [] and task.contract == {}

    def test_body_only_declaration(self) -> None:
        (task,) = parse_plan(_plan(changes_api=False))
        assert task.changes_api is False

    def test_contract_implies_an_api_change(self) -> None:
        (task,) = parse_plan(_plan(contract={"m.py::function::f": "def f(x: int)"}))
        assert task.changes_api is True
        assert task.contract == {NodeId("m.py::function::f"): "def f(x: int)"}

    def test_api_targets_imply_an_api_change(self) -> None:
        (task,) = parse_plan(_plan(api_targets=["m.py::function::g"]))
        assert task.changes_api is True
        assert task.api_targets == [NodeId("m.py::function::g")]

    def test_registry_keys_dedupe(self) -> None:
        (task,) = parse_plan(
            _plan(registry_keys={"m.py::function::g": ["/a", "/a", "/b"]})
        )
        assert task.registry_keys == {NodeId("m.py::function::g"): ["/a", "/b"]}

    @pytest.mark.parametrize(
        ("extra", "fragment"),
        [
            ({"changes_api": "yes"}, "changes_api"),
            ({"api_targets": ["other.py::function::x"]}, "does not target"),
            ({"contract": {"other.py::function::x": "def x()"}}, "does not target"),
            ({"contract": {"m.py::function::f": "not a sig"}}, "signature"),
            ({"contract": {"m.py::function::f": "def g()"}}, "declares 'g'"),
            ({"contract": {"m.py::function::f": 3}}, "non-empty strings"),
            (
                {"changes_api": False, "contract": {"m.py::function::f": "def f()"}},
                "changes_api' is false",
            ),
            ({"registry_keys": {"m.py::function::g": "k"}}, "registry_keys"),
            ({"registry_keys": {"x.py::function::r": ["k"]}}, "does not target"),
        ],
    )
    def test_unhonourable_declarations_are_refused(
        self, extra: dict[str, object], fragment: str
    ) -> None:
        with pytest.raises(ValueError, match=fragment):
            parse_plan(_plan(**extra))

    def test_plan_json_round_trips_declarations(self) -> None:
        tasks = parse_plan(
            _plan(
                changes_api=True,
                api_targets=["m.py::function::f"],
                contract={"m.py::function::f": "def f(x: int) -> int"},
                registry_keys={"m.py::function::g": ["k"]},
            )
        )
        assert parse_plan(_plan_to_json(tasks)) == tasks

    def test_undeclared_plan_json_has_no_new_keys(self) -> None:
        payload = json.loads(_plan_to_json(parse_plan(_plan())))
        assert set(payload[0]) == {
            "task_id", "description", "target_nodes", "context_nodes",
            "depends_on", "agent_type",
        }


class TestTaskGraphPersistence:
    def test_declarations_survive_recovery(self, tmp_path: Path) -> None:
        task = SubTask(
            "t",
            "d",
            target_nodes=[NodeId("m.py::function::f")],
            changes_api=False,
            registry_keys={NodeId("m.py::function::f"): ["k"]},
        )
        other = SubTask(
            "u",
            "d",
            target_nodes=[NodeId("n.py::function::h")],
            contract={NodeId("n.py::function::h"): "def h() -> int"},
            changes_api=True,
        )
        path = tmp_path / "task_graph.json"

        class _Runner:
            def assign(self, adapter: object, task: object) -> object:
                return None

        class _Registry:
            def get(self, agent_type: str) -> object:
                return object()

        class _Locks:
            def try_acquire_all(self, requests: object, holder: str) -> bool:
                return False

            def release_all(self, holder: str) -> int:
                return 0

        scheduler = Scheduler(
            DAG([task, other]), _Locks(), _Runner(), _Registry(),  # type: ignore[arg-type]
            persist_path=path,
        )
        scheduler.annotations["read_sets"] = {"t": {"x": 1}}
        scheduler.save()
        restored = Scheduler.from_persisted(
            path, _Locks(), _Runner(), _Registry()  # type: ignore[arg-type]
        )
        assert restored.dag.get_task("t") == task
        assert restored.dag.get_task("u") == other
        assert restored.annotations == {"read_sets": {"t": {"x": 1}}}

    def test_codec_round_trip_and_defaults(self) -> None:
        task = SubTask("t", "d", api_targets=[NodeId("a.py")], changes_api=True)
        assert subtask_from_dict(subtask_to_dict(task)) == task
        legacy = subtask_from_dict({"task_id": "x", "description": "y"})
        assert legacy == SubTask("x", "y")

    def test_codec_refuses_a_wrong_shape(self) -> None:
        with pytest.raises(TypeError):
            subtask_from_dict({"task_id": "x", "description": "y", "target_nodes": "a"})
