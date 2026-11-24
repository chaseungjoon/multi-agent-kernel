"""Wave 28: write sets that grow at commit — imports, new helpers, escalation.

Each test drives a real session with a scripted agent. Before Wave 28 every
case here lost output: an import meant WRITE on the module header (so importers
serialized), a helper beside a target was dropped as out of grant, and a node
the planner missed was dropped the same way.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from mak.config import SemanticConfig
from mak.core.logging import EventType
from mak.core.types import NodeId
from mak.session.results import plan_metrics
from mak.session.watchdog import LockWatchdog
from tests.semantic.helpers import (
    ScriptRunner,
    committed,
    events,
    logged,
    make_session,
    task,
)

M = "m.py"
HEADER = "m.py::module_header::__header__"
F = "m.py::function::f"
G = "m.py::function::g"
H = "m.py::function::h"

_BASE = (
    '"""Doc."""\nimport os\n\n\n'
    "def f():\n    return 0\n\n\n"
    "def g():\n    return 0\n\n\n"
    "def h():\n    return 0\n"
)


def _write(tmp_path: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _body(name: str, value: int) -> str:
    return f"def {name}():\n    return {value}\n"


def _all_dispatched(runner: ScriptRunner, *task_ids: str) -> Callable[[], bool]:
    return lambda: all(runner.calls.get(t, 0) for t in task_ids)


def _header(store: object) -> str:
    return store.get_node(NodeId(HEADER)).source  # type: ignore[attr-defined]


def _order(store: object, *ids: str) -> list[str]:
    return sorted(ids, key=lambda n: store.node_order(NodeId(n)))  # type: ignore[attr-defined]


class TestImportChannel:
    def test_three_importers_in_one_file_commit_concurrently(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner(
            {"a": [{F: _body("f", 1)}], "b": [{G: _body("g", 2)}],
             "c": [{H: _body("h", 3)}]},
            growth={
                "a": [{"imports": ["import json"]}],
                "b": [{"imports": ["import re"]}],
                "c": [{"imports": ["from typing import Any"]}],
            },
        )
        # Each agent answers only once all three are in flight: had the header
        # still serialized importers, the later ones could never be dispatched.
        for t in "abc":
            runner._hold[t] = _all_dispatched(runner, "a", "b", "c")
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("a", [F]), task("b", [G]), task("c", [H])])
        result = session.run()

        assert result.ok and sorted(result.completed) == ["a", "b", "c"]
        header = _header(store)
        for line in ("import os", "import json", "import re", "from typing import Any"):
            assert header.count(line + "\n") == 1, header
        assert header.startswith('"""Doc."""\nimport os\n')
        assert runner.calls == {"a": 1, "b": 1, "c": 1}
        assert session.wave.import_merges == 3
        assert len(events(logger, EventType.IMPORT_MERGED)) == 3
        text = (tmp_path / M).read_text()
        assert "import json" in text and "return 3" in text

    def test_the_same_import_twice_is_one_entry(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner(
            {"a": [{F: _body("f", 1)}], "b": [{G: _body("g", 2)}]},
            growth={
                "a": [{"imports": ["from pathlib import Path"]}],
                "b": [{"imports": ["from pathlib import Path"]}],
            },
        )
        session, store, _ = make_session(tmp_path, runner)
        runner._hold["b"] = committed(store, HEADER, "pathlib")
        session.initialize()
        session.install_plan([task("a", [F]), task("b", [G])])
        assert session.run().ok
        assert _header(store).count("from pathlib import Path") == 1
        # The second import was already there: nothing to merge.
        assert session.wave.import_merges == 1

    def test_a_name_bound_to_another_target_is_sent_back(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner(
            {"a": [{F: _body("f", 1)}], "b": [{G: _body("g", 2)}]},
            growth={
                "a": [{"imports": ["from os.path import join"]}],
                # b tries a clashing binding, then drops it once told why.
                "b": [{"imports": ["from shlex import join"]}, {}],
            },
        )
        session, store, logger = make_session(tmp_path, runner)
        runner._hold["b"] = committed(store, HEADER, "os.path")
        session.initialize()
        session.install_plan([task("a", [F]), task("b", [G])])
        assert session.run().ok
        header = _header(store)
        assert "from os.path import join" in header
        assert "shlex" not in header
        assert runner.calls["b"] == 2
        note = runner.bundles_for("b")[1].retry_note or ""
        assert "join" in note
        [conflict] = events(logger, EventType.GROWTH_CONFLICT)
        assert conflict.payload["task_id"] == "b"
        assert conflict.payload["kind"] == "import_conflict"

    def test_an_import_clashing_with_a_defined_name_is_sent_back(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner(
            {"a": [{F: _body("f", 1)}]},
            growth={"a": [{"imports": ["from x import g"]}, {}]},
        )
        session, store, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("a", [F])])
        assert session.run().ok
        assert "from x import g" not in _header(store)
        assert runner.calls["a"] == 2

    def test_importers_park_behind_a_header_writer_then_merge(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        new_header = '"""Doc."""\nimport os\n\nLIMIT = 10\n\n\n'
        runner = ScriptRunner(
            {"w": [{HEADER: new_header}], "i": [{F: _body("f", 1)}]},
            growth={"i": [{"imports": ["import json"]}]},
        )
        session, store, logger = make_session(tmp_path, runner)
        # The header writer answers only after the importer has parked on it.
        runner._hold["w"] = logged(logger, EventType.COMMIT_DEFERRED, task_id="i")
        session.initialize()
        session.install_plan([task("w", [HEADER]), task("i", [F])])
        result = session.run()

        assert result.ok and sorted(result.completed) == ["i", "w"]
        header = _header(store)
        assert "LIMIT = 10" in header and header.count("import json") == 1
        # Parked, not re-run: the importer's one reply was enough.
        assert runner.calls["i"] == 1

    def test_a_file_without_a_header_gets_one_before_its_first_node(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {"n.py": _body("a", 0) + "\n\n" + _body("b", 0)})
        a = "n.py::function::a"
        runner = ScriptRunner(
            {"t": [{a: _body("a", 1)}]},
            growth={"t": [{"imports": ["import json"]}]},
        )
        session, store, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [a])])
        assert session.run().ok
        header = NodeId("n.py::module_header::__header__")
        assert store.node_order(header) == 0
        assert store.node_order(NodeId(a)) == 1
        assert (tmp_path / "n.py").read_text().startswith("import json\n")

    def test_a_future_import_goes_first(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner(
            {"t": [{F: _body("f", 1)}]},
            growth={"t": [{"imports": ["from __future__ import annotations"]}]},
        )
        session, store, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        assert _header(store).startswith(
            '"""Doc."""\nfrom __future__ import annotations\nimport os\n'
        )

    def test_an_import_for_a_file_the_task_does_not_target_is_refused(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE, "o.py": "def z():\n    return 0\n"})
        runner = ScriptRunner(
            {"t": [{F: _body("f", 1)}]},
            growth={"t": [{"imports": ["o.py::import json"]}]},
        )
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        dropped = events(logger, EventType.SOURCE_DROPPED)
        assert any("o.py" in e.payload["reason"] for e in dropped)
        assert "json" not in (tmp_path / "o.py").read_text()

    def test_imports_written_into_the_header_without_a_grant_are_merged(
        self, tmp_path: Path
    ) -> None:
        # An agent that ignored the contract and rewrote the header to add an
        # import still gets its import; nothing else of the header changes.
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"t": [{
            F: _body("f", 1),
            HEADER: '"""Doc."""\nimport os\nimport json\n\n\n',
        }]})
        session, store, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        assert _header(store).count("import json") == 1
        assert session.wave.import_merges == 1


class TestAdditiveSymbols:
    def test_a_helper_lands_right_after_its_target(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        helper = "m.py::function::_helper"
        runner = ScriptRunner({"t": [{
            F: "def f():\n    return _helper()\n",
            helper: "def _helper():\n    return 1\n",
        }]})
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        assert _order(store, F, G, H, helper) == [F, helper, G, H]
        text = (tmp_path / M).read_text()
        assert text.index("def _helper") < text.index("def g")
        assert session.wave.additive_symbols == 1
        assert len(events(logger, EventType.ADDITIVE_SYMBOL)) == 1
        assert runner.calls["t"] == 1

    def test_insert_after_places_a_helper_explicitly(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        helper = "m.py::function::_helper"
        runner = ScriptRunner(
            {"t": [{F: _body("f", 1), helper: _body("_helper", 2)}]},
            growth={"t": [{"placements": {helper: G}}]},
        )
        session, store, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        assert _order(store, F, G, H, helper) == [F, G, helper, H]

    def test_two_helpers_keep_their_reply_order(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        one, two = "m.py::function::_one", "m.py::function::_two"
        runner = ScriptRunner({"t": [{
            F: _body("f", 1), one: _body("_one", 1), two: _body("_two", 2),
        }]})
        session, store, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        assert _order(store, F, G, one, two) == [F, one, two, G]

    def test_an_existing_id_is_escalated_not_added(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"t": [{F: _body("f", 1), G: _body("g", 7)}]})
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        assert not events(logger, EventType.ADDITIVE_SYMBOL)
        assert events(logger, EventType.ESCALATION)
        assert "return 7" in store.get_node(NodeId(G)).source

    def test_a_retired_id_is_escalated_not_added(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"t": [{F: _body("f", 1), G: _body("g", 7)}]})
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        assert store.retire_node(NodeId(G))
        session.install_plan([task("t", [F])])
        session.run()
        assert not events(logger, EventType.ADDITIVE_SYMBOL)
        assert any(
            G in e.payload["nodes"] for e in events(logger, EventType.ESCALATION)
        )

    def test_past_the_cap_a_helper_is_refused_with_a_reason(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        helpers = {f"m.py::function::_h{i}": _body(f"_h{i}", i) for i in range(6)}
        runner = ScriptRunner({"t": [{F: _body("f", 1), **helpers}]})
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        assert session.wave.additive_symbols == 5
        refused = [
            e for e in events(logger, EventType.SOURCE_DROPPED)
            if "max_additive_symbols" in e.payload["reason"]
        ]
        assert [e.payload["node_id"] for e in refused] == ["m.py::function::_h5"]
        assert store.node_order(NodeId("m.py::function::_h5")) is None

    def test_a_helper_created_meanwhile_is_escalated_and_may_be_left_out(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        helper = "m.py::function::_shared"
        runner = ScriptRunner({
            "a": [{F: _body("f", 1), helper: _body("_shared", 1)}],
            "b": [{G: _body("g", 2), helper: _body("_shared", 2)}, {G: _body("g", 2)}],
        })
        session, store, _ = make_session(tmp_path, runner)
        runner._hold["b"] = committed(store, helper, "return 1")
        session.initialize()
        session.install_plan([task("a", [F]), task("b", [G])])
        assert session.run().ok
        assert "return 1" in store.get_node(NodeId(helper)).source
        # By b's commit the helper existed, so b's copy asked to overwrite it.
        # b was sent the current source and its second reply left it alone:
        # an escalated node is granted, never owed.
        assert runner.calls["b"] == 2
        assert "return 2" in store.get_node(NodeId(G)).source

    def test_two_helpers_of_one_name_in_flight_keep_one_copy(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        helper = "m.py::function::_shared"
        runner = ScriptRunner({
            "a": [{F: _body("f", 1), helper: _body("_shared", 1)}, {F: _body("f", 1)}],
            "b": [{G: _body("g", 2), helper: _body("_shared", 2)}, {G: _body("g", 2)}],
        })
        both = _all_dispatched(runner, "a", "b")
        runner._hold["a"] = both
        runner._hold["b"] = both
        session, store, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("a", [F]), task("b", [G])])
        assert session.run().ok
        assert (tmp_path / M).read_text().count("def _shared") == 1
        # The loser is told the name was taken, not that it asked for a node.
        notes = [b.retry_note or "" for b in runner.bundles if b.retry_note]
        assert any("rename it" in note for note in notes)


    def test_without_intention_locks_a_helper_is_refused(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        helper = "m.py::function::_helper"
        runner = ScriptRunner({"t": [{F: _body("f", 1), helper: _body("_helper", 1)}]})
        session, store, logger = make_session(
            tmp_path, runner, semantic=SemanticConfig(intention_locks=False)
        )
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        (refused,) = events(logger, EventType.SOURCE_DROPPED)
        assert "intention_locks" in refused.payload["reason"]
        assert store.node_order(NodeId(helper)) is None

    def test_a_new_method_without_its_class_is_refused_with_a_reason(
        self, tmp_path: Path
    ) -> None:
        cls = "\n\nclass C:\n    def m(self):\n        return 0\n"
        _write(tmp_path, {M: _BASE + cls})
        method = "m.py::method::C.extra"
        runner = ScriptRunner(
            {"t": [{F: _body("f", 1), method: "def extra(self): ...\n"}]}
        )
        session, _, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        (refused,) = events(logger, EventType.SOURCE_DROPPED)
        assert refused.payload["node_id"] == method
        assert "needs its class granted" in refused.payload["reason"]


class TestEscalation:
    def test_a_free_node_the_agent_saw_commits_in_the_same_attempt(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"t": [{F: _body("f", 1), G: _body("g", 9)}]})
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        assert runner.calls["t"] == 1
        assert "return 9" in store.get_node(NodeId(G)).source
        (granted,) = events(logger, EventType.ESCALATION)
        assert granted.payload["outcome"] == "granted"
        assert session.wave.escalations_granted == 1
        metrics = plan_metrics(session.wave)
        assert metrics["escalations_requested"] == 1
        assert metrics["escalations_granted"] == 1
        assert {"import_merges", "additive_symbols", "escalation_restarts"} <= set(
            metrics
        )

    def test_a_free_node_the_agent_never_saw_is_resent_with_it(
        self, tmp_path: Path
    ) -> None:
        z = "o.py::function::z"
        _write(tmp_path, {M: _BASE, "o.py": "def z():\n    return 0\n"})
        runner = ScriptRunner(
            {"t": [{F: _body("f", 1)}, {F: _body("f", 1), z: _body("z", 5)}]},
            growth={"t": [{"needs_nodes": [(z, "write")]}, {}]},
        )
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        assert runner.calls["t"] == 2
        second = runner.bundles_for("t")[1]
        assert NodeId(z) in second.target_nodes
        assert "def z" in "".join(second.context.values())
        assert "return 5" in store.get_node(NodeId(z)).source
        outcomes = [e.payload["outcome"] for e in events(logger, EventType.ESCALATION)]
        assert outcomes == ["granted_resend"]

    def test_a_busy_node_restarts_the_task_holding_nothing(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({
            "a": [{F: _body("f", 1), G: _body("g", 7)},
                  {F: _body("f", 1), G: "def g():\n    return 70\n"}],
            "b": [{G: _body("g", 2)}],
        })
        session, store, logger = make_session(tmp_path, runner)
        restarted = logged(logger, EventType.ESCALATION, task_id="a",
                           outcome="restarted")
        holds: list[list[str]] = []

        def b_waits_for_restart() -> bool:
            # a is back in the ready queue, and cannot leave it while b holds g.
            queue = session.wave.require_scheduler().ready_queue
            if restarted() and any(t.task_id == "a" for t in list(queue)):
                table = session._lock_table  # type: ignore[attr-defined]
                holds.append([
                    str(node) for node, entries in table.all_entries().items()
                    if any(e.holder == "a" for e in entries)
                ])
                return True
            return False

        runner._hold["b"] = b_waits_for_restart
        session.initialize()
        session.install_plan([task("a", [F]), task("b", [G])])
        result = session.run()

        assert result.ok and sorted(result.completed) == ["a", "b"]
        # Waiting for g, which b held, a held nothing at all: no hold-and-wait.
        assert holds and holds[0] == []
        second = runner.bundles_for("a")[1]
        assert NodeId(G) in second.target_nodes
        assert "previous reply" in (second.retry_note or "")
        assert "return 70" in store.get_node(NodeId(G)).source
        assert session.wave.escalation_restarts == 1
        task_a = session._wave.require_scheduler().dag.get_task("a")  # type: ignore[attr-defined]
        assert NodeId(G) in task_a.target_nodes

    def test_a_whole_file_escalation_restarts_with_that_target(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, {M: _BASE, "o.py": "def z():\n    return 0\n"})
        runner = ScriptRunner(
            {"t": [{F: _body("f", 1)},
                   {F: _body("f", 1), "o.py": "def z():\n    return 3\n"}]},
            growth={"t": [{"needs_nodes": [("o.py", "write")]}, {}]},
        )
        session, store, logger = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        (event,) = events(logger, EventType.ESCALATION)
        assert event.payload["outcome"] == "restarted"
        assert NodeId("o.py") in runner.bundles_for("t")[1].target_nodes
        assert "return 3" in (tmp_path / "o.py").read_text()

    def test_escalations_stop_at_the_cap(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        runner = ScriptRunner({"t": [{F: _body("f", 1), G: _body("g", 7)}]})
        session, store, logger = make_session(
            tmp_path, runner, session_overrides={"max_escalations": 0}
        )
        session.initialize()
        session.install_plan([task("t", [F])])
        assert session.run().ok
        # The pre-Wave-28 behaviour, now opt-in: the target commits, g does not.
        assert "return 1" in store.get_node(NodeId(F)).source
        assert "return 0" in store.get_node(NodeId(G)).source
        (event,) = events(logger, EventType.ESCALATION)
        assert event.payload["outcome"] == "refused"

    def test_recover_keeps_the_widened_targets(self, tmp_path: Path) -> None:
        _write(tmp_path, {M: _BASE})
        gate = threading.Event()
        runner = ScriptRunner({
            "a": [{F: _body("f", 1), G: _body("g", 7)}],
            "b": [{G: _body("g", 2)}],
        })
        runner._hold["b"] = gate.is_set
        session, _, _ = make_session(tmp_path, runner)
        session.initialize()
        session.install_plan([task("a", [F]), task("b", [G])])
        scheduler = session.wave.require_scheduler()
        scheduler.tick()
        batch = session.batches.collect()  # a answers; b is still held
        session.batches.process_batch(session.wave, batch)
        assert "a" in session.wave.restarted or NodeId(G) in (
            scheduler.dag.get_task("a").target_nodes
        )
        gate.set()
        session.close()  # crash before a runs again

        resumed, _, _ = make_session(tmp_path, ScriptRunner({}))
        resumed.recover()
        dag = resumed.wave.require_scheduler().dag
        assert NodeId(G) in dag.get_task("a").target_nodes
        rounds = resumed.wave.require_scheduler().annotations["escalations"]
        assert rounds == {"a": [[G]]}  # type: ignore[comparison-overlap]


class TestNoHoldAndWait:
    def test_crossing_escalations_never_deadlock(self, tmp_path: Path) -> None:
        """Every task writes its neighbour's node: a ring of escalations.

        With hold-and-wait this is the textbook deadlock. The watchdog runs on
        every loop tick and the invariant is checked there: a task waiting in
        the ready queue holds no lock.
        """
        n = 8
        names = [f"f{i}" for i in range(n)]
        source = "\n\n".join(_body(name, 0) for name in names)
        _write(tmp_path, {M: source})
        ids = [f"m.py::function::{name}" for name in names]
        script = {
            f"t{i}": [{ids[i]: _body(names[i], 100 + i),
                       ids[(i + 1) % n]: _body(names[(i + 1) % n], 100 + i)}]
            for i in range(n)
        }
        runner = ScriptRunner(script)
        session, store, logger = make_session(
            tmp_path, runner, concurrency=n,
            session_overrides={"deadlock_check_interval_s": 0.0},
        )
        violations: list[str] = []
        original = LockWatchdog.check_deadlocks

        def checked(self: LockWatchdog, wave: object) -> None:
            scheduler = wave.scheduler  # type: ignore[attr-defined]
            if scheduler is not None:
                table = self._lock_table  # type: ignore[attr-defined]
                holders = {
                    e.holder for entries in table.all_entries().values()
                    for e in entries
                }
                violations.extend(
                    t.task_id for t in scheduler.ready_queue if t.task_id in holders
                )
            original(self, wave)  # type: ignore[arg-type]

        LockWatchdog.check_deadlocks = checked  # type: ignore[method-assign]
        try:
            session.initialize()
            session.install_plan([task(f"t{i}", [ids[i]]) for i in range(n)])
            result = session.run()
        finally:
            LockWatchdog.check_deadlocks = original  # type: ignore[method-assign]

        assert result.ok and len(result.completed) == n
        assert violations == []
        assert not [
            e for e in events(logger, EventType.CONFLICT_DETECTED)
            if "deadlock" in e.payload
        ]
        # Each node ends as its own task or its left neighbour wrote it.
        for i in range(n):
            text = store.get_node(NodeId(ids[i])).source
            assert f"return {100 + i}" in text or f"return {100 + i - 1}" in text \
                or (i == 0 and f"return {100 + n - 1}" in text)
        assert session.wave.escalations_requested >= n
