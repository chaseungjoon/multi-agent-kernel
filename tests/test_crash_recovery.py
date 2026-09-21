"""Corrupt persisted state must never stop the next run from starting.

All three of MAK's state files used to be written with a plain ``write_text``,
which truncates before writing. A kill between the two left valid JSON replaced
by a fragment, and every reader raised straight out of the constructor — so the
node store became unopenable and ``--recover`` broke on exactly the crash it
exists to handle.

Each file gets the policy that fits what losing it costs, and these tests pin
those policies rather than the mechanism.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mak.core.exceptions import SchedulingError
from mak.core.types import LockMode, NodeFragment, NodeId, SubTask
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.scheduler.dag import DAG
from mak.scheduler.scheduler import Scheduler

# Truncation points chosen to land mid-token, mid-string, and mid-structure.
TRUNCATIONS = [0, 1, 5, 17, 40]


class _Registry:
    def get(self, agent_type: str) -> object:
        return object()


class _Runner:
    def assign(self, adapter: object, task: object) -> object:
        return None


def _scheduler(path: Path) -> Scheduler:
    tasks = [
        SubTask(
            task_id="t1",
            description="d",
            target_nodes=[NodeId("a.py::function::f")],
            context_nodes=[],
            depends_on=[],
            agent_type="fake",
        )
    ]
    return Scheduler(
        DAG(tasks),
        LockTable(),  # type: ignore[arg-type]
        _Runner(),
        _Registry(),
        persist_path=path,
    )


class TestLockTableSurvivesCorruption:
    """A lost lock table costs a re-acquire, so it starts empty and carries on."""

    @pytest.mark.parametrize("cut", TRUNCATIONS)
    def test_truncated_table_starts_empty(self, tmp_path: Path, cut: int) -> None:
        path = tmp_path / "lock_table.json"
        table = LockTable(persist_path=path)
        table.try_acquire(NodeId("a.py::function::f"), LockMode.WRITE, "t1")
        path.write_text(path.read_text()[:cut], encoding="utf-8")

        reloaded = LockTable(persist_path=path)
        assert reloaded.all_entries() == {}
        # And it is still usable, not merely non-crashing.
        assert reloaded.try_acquire(NodeId("a.py::function::f"), LockMode.WRITE, "t2")

    def test_wrong_toplevel_type_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "lock_table.json"
        path.write_text('{"not": "an array"}', encoding="utf-8")
        assert LockTable(persist_path=path).all_entries() == {}

    def test_entry_missing_a_field_starts_empty(self, tmp_path: Path) -> None:
        # This used to raise a bare KeyError past the JSON handler and abort.
        path = tmp_path / "lock_table.json"
        path.write_text(json.dumps([{"node_id": "a.py", "mode": "write"}]))
        assert LockTable(persist_path=path).all_entries() == {}

    def test_unknown_lock_mode_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "lock_table.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "node_id": "a.py",
                        "mode": "sideways",
                        "holder": "t",
                        "acquired_at": 1,
                    }
                ]
            )
        )
        assert LockTable(persist_path=path).all_entries() == {}


class TestTaskGraphSurvivesCorruption:
    """An unreadable graph is reported, so --recover can say 'nothing to resume'."""

    @pytest.mark.parametrize("cut", TRUNCATIONS)
    def test_truncated_graph_raises_a_domain_error(
        self, tmp_path: Path, cut: int
    ) -> None:
        path = tmp_path / "task_graph.json"
        _scheduler(path)
        path.write_text(path.read_text()[:cut], encoding="utf-8")

        with pytest.raises(SchedulingError):
            Scheduler.from_persisted(path, LockTable(), _Runner(), _Registry())

    def test_malformed_task_entry_raises_a_domain_error(self, tmp_path: Path) -> None:
        path = tmp_path / "task_graph.json"
        path.write_text(json.dumps({"tasks": [{"description": "no id"}]}))
        with pytest.raises(SchedulingError, match="malformed"):
            Scheduler.from_persisted(path, LockTable(), _Runner(), _Registry())

    def test_intact_graph_still_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "task_graph.json"
        _scheduler(path)
        restored = Scheduler.from_persisted(path, LockTable(), _Runner(), _Registry())
        assert list(restored.dag.tasks) == ["t1"]

    def test_objective_and_cascade_history_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "task_graph.json"
        scheduler = _scheduler(path)
        scheduler.annotations.update(
            {
                "objective": "embed every separated track",
                "cascade_history": ["state-a", "state-b"],
            }
        )
        scheduler.save()

        restored = Scheduler.from_persisted(path, LockTable(), _Runner(), _Registry())

        assert restored.annotations["objective"] == "embed every separated track"
        assert restored.annotations["cascade_history"] == ["state-a", "state-b"]


class TestNodeStoreSurvivesCorruption:
    """Fragments are the valuable part; a bad index is quarantined, not fatal."""

    @pytest.mark.parametrize("cut", TRUNCATIONS)
    def test_truncated_metadata_opens_clean(self, tmp_path: Path, cut: int) -> None:
        root = tmp_path / "store"
        store = NodeStore(root)
        nid = NodeId("a.py::function::f")
        store.put_node(
            nid, NodeFragment(nid, "function", "def f():\n    return 1\n", 1)
        )
        store.commit_node(nid)

        meta = root / "metadata.json"
        meta.write_text(meta.read_text()[:cut], encoding="utf-8")

        reopened = NodeStore(root)
        assert reopened.list_all_nodes() == []
        # The bad index is kept for inspection, and the fragments are untouched.
        assert (root / "metadata.json.corrupt").exists()
        assert (root / "a.py" / "function" / "f" / "v1.py").exists()

    def test_a_reopened_store_is_writable(self, tmp_path: Path) -> None:
        root = tmp_path / "store"
        NodeStore(root)
        (root / "metadata.json").write_text("{not json", encoding="utf-8")

        store = NodeStore(root)
        nid = NodeId("b.py::function::g")
        store.put_node(
            nid, NodeFragment(nid, "function", "def g():\n    return 2\n", 1)
        )
        store.commit_node(nid)
        assert store.get_node(nid).source.startswith("def g()")

    def test_non_object_metadata_is_ignored(self, tmp_path: Path) -> None:
        root = tmp_path / "store"
        NodeStore(root)
        (root / "metadata.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert NodeStore(root).list_all_nodes() == []


class TestRecoverDegradesGracefully:
    """--recover reports rather than raising on the crash it exists to handle."""

    def test_a_corrupt_graph_leaves_the_session_unplanned(
        self, tmp_path: Path
    ) -> None:
        from mak.node_store.store import NodeStore as _Store
        from mak.session import SessionState
        from tests.test_session import _session

        mak_dir = tmp_path / ".mak"
        mak_dir.mkdir()
        (mak_dir / "task_graph.json").write_text("{truncated", encoding="utf-8")

        session = _session(
            tmp_path, runner=_Runner(), node_store=_Store(mak_dir / "node_store")
        )
        # No exception: the caller checks the state and says "nothing to recover".
        session.recover()
        assert session.state is not SessionState.PLANNED

    def test_an_intact_graph_still_recovers(self, tmp_path: Path) -> None:
        from mak.node_store.store import NodeStore as _Store
        from mak.session import SessionState
        from tests.test_session import _session

        mak_dir = tmp_path / ".mak"
        mak_dir.mkdir()
        _scheduler(mak_dir / "task_graph.json")

        session = _session(
            tmp_path, runner=_Runner(), node_store=_Store(mak_dir / "node_store")
        )
        session.recover()
        assert session.state is SessionState.PLANNED

    def test_recover_restores_objective_and_cascade_history(
        self, tmp_path: Path
    ) -> None:
        from mak.node_store.store import NodeStore as _Store
        from tests.test_session import _session

        mak_dir = tmp_path / ".mak"
        mak_dir.mkdir()
        scheduler = _scheduler(mak_dir / "task_graph.json")
        scheduler.annotations.update(
            {
                "objective": "embed every separated track",
                "cascade_history": ["state-a"],
            }
        )
        scheduler.save()
        session = _session(
            tmp_path, runner=_Runner(), node_store=_Store(mak_dir / "node_store")
        )

        session.recover()

        assert session._objective == "embed every separated track"
        assert session.cascade_history() == ("state-a",)


class TestWritesAreAtomic:
    """No state file is ever observed as a truncation while being rewritten."""

    def test_no_temp_debris_after_normal_operation(self, tmp_path: Path) -> None:
        path = tmp_path / "lock_table.json"
        table = LockTable(persist_path=path)
        for i in range(5):
            table.try_acquire(NodeId(f"a.py::function::f{i}"), LockMode.WRITE, "t1")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["lock_table.json"]
        assert json.loads(path.read_text())  # still parseable
