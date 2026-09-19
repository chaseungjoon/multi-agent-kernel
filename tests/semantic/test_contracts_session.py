"""Wave 20 declared contracts in a running session (P3)."""

from __future__ import annotations

from pathlib import Path

from mak.config import SemanticConfig
from mak.core.logging import EventType
from mak.core.types import NodeId, SubTask
from mak.scheduler.lock_policy import LockPolicy
from mak.semantic.contracts import soft_edges, visible_contracts
from tests.semantic.helpers import ScriptRunner, events, make_session, task

API = "api.py::function::fetch"
USE = "use.py::function::go"
CONTRACT = {NodeId(API): "def fetch(uid: int, region: str) -> dict"}
IMPL = "def fetch(uid: int, region: str) -> dict:\n    return {'uid': uid}\n"
CALLER = "from api import fetch\n\ndef go():\n    return fetch(1, 'eu')\n"


def _files(tmp_path: Path) -> None:
    (tmp_path / "api.py").write_text("def fetch(uid):\n    return {}\n")
    (tmp_path / "use.py").write_text("def go():\n    return 0\n")


def _plan(**dependent: object) -> list[SubTask]:
    return [
        task("p", [API], contract=CONTRACT),
        task("d", [USE], deps=["p"], changes_api=False, **dependent),
    ]


class TestLayerZero:
    def test_provider_and_dependent_both_see_the_contract(
        self, tmp_path: Path
    ) -> None:
        _files(tmp_path)
        runner = ScriptRunner({
            "p": [{API: IMPL}],
            "d": [{USE: CALLER}],
        })
        session, _, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan(_plan())
        assert session.run().ok
        key = f"contract:{API}"
        own = runner.bundles_for("p")[0].context[key]
        theirs = runner.bundles_for("d")[0].context[key]
        assert "you must implement it" in own
        assert "build against exactly this signature" in theirs
        assert "def fetch(uid: int, region: str) -> dict" in theirs


class TestProviderCheck:
    def test_an_implementation_that_drifts_is_sent_back(self, tmp_path: Path) -> None:
        _files(tmp_path)
        runner = ScriptRunner({"p": [
            {API: "def fetch(uid: int) -> dict:\n    return {}\n"},
            {API: IMPL},
        ]})
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("p", [API], contract=CONTRACT)])
        assert session.run().ok
        assert runner.calls["p"] == 2
        (event,) = events(logger, EventType.CONTRACT_VIOLATION)
        assert "declared" in str(event.payload["reason"])
        note = runner.bundles_for("p")[1].retry_note
        assert note is not None and "declared contract" in note
        assert store.get_node(NodeId(API)).source == IMPL


class TestDependentCheck:
    def test_a_call_that_breaks_the_contract_is_rejected(self, tmp_path: Path) -> None:
        _files(tmp_path)
        runner = ScriptRunner({
            "p": [{API: IMPL}],
            "d": [
                {USE: "from api import fetch\n\ndef go():\n    return fetch(1)\n"},
                {USE: CALLER},
            ],
        })
        session, _, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan(_plan())
        assert session.run().ok
        assert runner.calls["d"] == 2
        reasons = [
            str(r) for e in events(logger, EventType.CONFLICT_DETECTED)
            for r in e.payload.get("reasons", [])  # type: ignore[union-attr]
        ]
        assert any("missing required argument 'region'" in r for r in reasons)


class TestContractDispatch:
    def test_dependent_runs_ahead_and_commits_after_its_provider(
        self, tmp_path: Path
    ) -> None:
        _files(tmp_path)
        seen_by_d: list[str] = []

        def dependent(bundle: object) -> str:
            seen_by_d.append(
                bundle.context.get(f"write_source:{API}", "")  # type: ignore[attr-defined]
            )
            return "from api import fetch\n\ndef go():\n    return fetch(1, 'eu')\n"

        runner = ScriptRunner({"p": [{API: IMPL}], "d": [{USE: dependent}]})
        session, store, logger = make_session(
            tmp_path, runner, semantic=SemanticConfig(contract_dispatch=True)
        )
        # The provider waits until the dependent has been dispatched: proof the
        # dependent did not wait for the implementation.
        runner._hold["p"] = lambda: runner.calls.get("d", 0) > 0
        session.initialize()
        session.install_plan(_plan(context=[API]))
        result = session.run()
        assert result.ok, result.failure_reasons
        assert runner.calls == {"p": 1, "d": 1}
        deferred = [
            e for e in events(logger, EventType.COMMIT_DEFERRED)
            if e.payload["task_id"] == "d" and "reason" in e.payload
        ]
        assert deferred and "contract" in str(deferred[0].payload["reason"])
        assert "fetch(1, 'eu')" in store.get_node(NodeId(USE)).source

    def test_dependent_fails_when_its_provider_fails(self, tmp_path: Path) -> None:
        _files(tmp_path)
        runner = ScriptRunner({
            "p": [{API: "def fetch(uid):\n    return 1\n"}],  # never matches
            "d": [{USE: "def go():\n    return 1\n"}],
        })
        session, _, _ = make_session(
            tmp_path, runner, semantic=SemanticConfig(contract_dispatch=True),
            max_attempts=2,
        )
        runner._hold["p"] = lambda: runner.calls.get("d", 0) > 0
        session.initialize()
        session.install_plan(_plan())
        result = session.run()
        assert not result.ok
        assert "p" in result.failed
        assert "d" in result.failed or "d" in result.skipped

    def test_off_by_default_the_edge_gates_dispatch(self, tmp_path: Path) -> None:
        _files(tmp_path)
        order: list[str] = []

        def record(name: str, source: str) -> object:
            def build(bundle: object) -> str:
                order.append(name)
                return source
            return build

        runner = ScriptRunner({
            "p": [{API: record("p", IMPL)}],
            "d": [{USE: record("d", "def go():\n    return 1\n")}],
        })
        session, _, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan(_plan())
        assert session.run().ok
        assert order == ["p", "d"]


class TestSoftEdgeRules:
    def test_only_fully_contracted_non_conflicting_providers_soften(self) -> None:
        provider = SubTask("p", "d", [NodeId(API)], contract=CONTRACT)
        partial = SubTask(
            "q", "d", [NodeId(API), NodeId("api.py::function::other")],
            contract=CONTRACT,
        )
        dependent = SubTask("d", "d", [NodeId(USE)], depends_on=["p", "q"])
        _, edges = soft_edges([provider, partial, dependent], LockPolicy())
        assert edges == {"d": {"p"}}

    def test_softening_drops_the_providers_targets_from_context(self) -> None:
        provider = SubTask("p", "d", [NodeId(API)], contract=CONTRACT)
        reader = SubTask(
            "d", "d", [NodeId(USE)], context_nodes=[NodeId(API), NodeId("x.py")],
            depends_on=["p"],
        )
        plan, edges = soft_edges([provider, reader], LockPolicy())
        assert edges == {"d": {"p"}}
        assert plan[1].context_nodes == [NodeId("x.py")]

    def test_conflicting_locks_keep_the_edge_hard(self) -> None:
        # The provider reads what the dependent writes: the dependent would
        # hold that WRITE while parked, and the provider could never start.
        provider = SubTask(
            "p", "d", [NodeId(API)], context_nodes=[NodeId(USE)], contract=CONTRACT
        )
        dependent = SubTask("d", "d", [NodeId(USE)], depends_on=["p"])
        plan, edges = soft_edges([provider, dependent], LockPolicy())
        assert edges == {} and plan[1] == dependent

    def test_visible_contracts(self) -> None:
        provider = SubTask("p", "d", [NodeId(API)], contract=CONTRACT)
        stranger = SubTask("s", "d", [NodeId("s.py")], contract={
            NodeId("s.py"): "def s()"
        })
        reader = SubTask("r", "d", [NodeId(USE)], depends_on=["p"],
                         context_nodes=[NodeId("s.py")])
        plan = {t.task_id: t for t in (provider, stranger, reader)}
        assert set(visible_contracts(reader, plan)) == {NodeId(API), NodeId("s.py")}
