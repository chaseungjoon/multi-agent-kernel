"""Wave 10 acceptance (PLANS §16.4): deterministic plan augmentation end-to-end.

Builds a small real project, ingests it with a real ``NodeStore``, and drives a real
``Session`` whose planner emits a deliberately bad plan (a missing dependency edge and
a hallucinated node id). Asserts the static dependency graph grounds the hallucination
and adds the missing edge before dispatch, the run completes, and metrics are logged.
"""

from __future__ import annotations

import json
from pathlib import Path

from mak.config import GitConfig, MakConfig, NodeStoreConfig, SessionConfig
from mak.core.logging import EventType, SessionLogger
from mak.core.types import NodeFragment, NodeId, TaskBundle, TaskResult
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.planner.planner import Planner
from mak.session import Session, SessionState

_UTIL = "def parse_config(s):\n    return s\n"
_APP = "from util import parse_config\n\n\ndef load():\n    return parse_config('x')\n"
_REPORT = "from app import load\n\n\ndef render():\n    return load()\n"

# A bad plan: 'edit-load' declares no dependency on 'edit-parse' (even though load
# calls parse_config) and targets a hallucinated id 'app.py::function::lod'.
_BAD_PLAN = json.dumps(
    [
        {
            "task_id": "edit-parse",
            "description": "rewrite parse_config",
            "target_nodes": ["util.py::function::parse_config"],
            "depends_on": [],
            "agent_type": "",
        },
        {
            "task_id": "edit-load",
            "description": "rewrite load",
            "target_nodes": ["app.py::function::lod"],
            "depends_on": [],
            "agent_type": "",
        },
    ]
)


class _StubLLM:
    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, prompt: str) -> str:
        return self._response


class _Registry:
    def get(self, agent_type: str) -> object:
        return object()

    def list_types(self) -> list[str]:
        return ["anthropic_api"]


class _StagingRunner:
    """A fake agent that stages ``x = 1`` for each of a task's target nodes."""

    def __init__(self, store: NodeStore) -> None:
        self._store = store

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        done: list[NodeId] = []
        for node_id in task.target_nodes:
            self._store.put_node(
                node_id, NodeFragment(node_id, "function", "x = 1\n", 1)
            )
            done.append(node_id)
        return TaskResult(task_id=task.task_id, success=True, modified_nodes=done)


def _config(tmp_path: Path) -> MakConfig:
    return MakConfig(
        session=SessionConfig(work_dir=str(tmp_path), mak_dir=str(tmp_path / ".mak")),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(),
    )


def test_wave10_grounds_and_augments_bad_plan(tmp_path: Path) -> None:
    (tmp_path / "util.py").write_text(_UTIL)
    (tmp_path / "app.py").write_text(_APP)
    (tmp_path / "report.py").write_text(_REPORT)

    store = NodeStore(tmp_path / ".mak" / "node_store")
    logger = SessionLogger(tmp_path / ".mak" / "session.jsonl")
    session = Session(
        session_id="wave10",
        config=_config(tmp_path),
        node_store=store,
        lock_table=LockTable(),
        registry=_Registry(),  # type: ignore[arg-type]
        agent_runner=_StagingRunner(store),  # type: ignore[arg-type]
        planner=Planner(_StubLLM(_BAD_PLAN)),
        logger=logger,
    )

    session.initialize()
    installed = session.plan("rework config handling", review=False)

    # The missing edge was added from the real call graph.
    edit_load = next(t for t in installed if t.task_id == "edit-load")
    assert edit_load.depends_on == ["edit-parse"]
    # The hallucinated id was grounded to the real node.
    assert NodeId("app.py::function::load") in edit_load.target_nodes
    assert NodeId("app.py::function::lod") not in edit_load.target_nodes

    kinds = {f.kind for f in session.last_plan_findings}
    assert "missing_dep" in kinds
    assert "corrected_node" in kinds

    result = session.run()
    assert result.ok
    assert session.state is SessionState.COMPLETED
    assert result.metrics
    assert result.metrics["tasks_completed"] == 2

    metric_events = [
        e for e in logger.read_log() if e.event_type is EventType.PLAN_METRICS
    ]
    assert len(metric_events) == 1
