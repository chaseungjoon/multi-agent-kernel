"""End-to-end integration: ingest → plan → dispatch → commit → reconstruct.

Drives a real ``Session`` over the real node store, lock table, scheduler, and
conflict detector, with a mock agent that edits one function. Verifies the file is
reconstructed on disk with **every comment intact** — the load-bearing property of
the AST pipeline (the Wave 4 completion criterion).
"""

from __future__ import annotations

from pathlib import Path

from mak.config import GitConfig, MakConfig, NodeStoreConfig, SessionConfig
from mak.core.types import NodeFragment, NodeId, SubTask, TaskBundle, TaskResult
from mak.lock_manager.lock_table import LockTable
from mak.node_store.store import NodeStore
from mak.session import Session, SessionState

_SAMPLE = '''\
# module header comment
import os  # inline import comment

GREETING = "hello"  # a module-level constant


def greet(name):
    # greet body comment (should be replaced)
    return f"{GREETING} {name}"


def farewell(name):
    # farewell body comment (must survive untouched)
    return f"bye {name}"
'''

_NEW_GREET = (
    "def greet(name):\n"
    "    # NEW comment written by the agent\n"
    '    return f"{GREETING}, {name}!"\n'
)


class _Registry:
    def get(self, agent_type: str) -> object:
        return object()


class _EditGreetRunner:
    """A mock agent that rewrites the ``greet`` function, preserving a comment."""

    def __init__(self, store: NodeStore) -> None:
        self._store = store

    def assign(self, adapter: object, task: TaskBundle) -> TaskResult:
        done: list[NodeId] = []
        for node_id in task.target_nodes:
            self._store.put_node(
                node_id, NodeFragment(node_id, "function", _NEW_GREET, 1)
            )
            done.append(node_id)
        return TaskResult(task_id=task.task_id, success=True, modified_nodes=done)


def _config(tmp_path: Path) -> MakConfig:
    return MakConfig(
        session=SessionConfig(
            work_dir=str(tmp_path), mak_dir=str(tmp_path / ".mak")
        ),
        git=GitConfig(auto_commit=False, auto_push=False),
        node_store=NodeStoreConfig(),
    )


def test_run_preserves_comments_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text(_SAMPLE)
    store = NodeStore(tmp_path / ".mak" / "node_store")
    session = Session(
        session_id="it-1",
        config=_config(tmp_path),
        node_store=store,
        lock_table=LockTable(),
        registry=_Registry(),  # type: ignore[arg-type]
        agent_runner=_EditGreetRunner(store),  # type: ignore[arg-type]
    )

    inventory = session.initialize()
    assert NodeId("sample.py::function::greet") in inventory
    assert NodeId("sample.py::function::farewell") in inventory

    session.install_plan(
        [
            SubTask(
                task_id="edit-greet",
                description="Rewrite greet",
                target_nodes=[NodeId("sample.py::function::greet")],
                agent_type="fake",
            )
        ]
    )
    result = session.run()

    assert result.ok
    assert session.state is SessionState.COMPLETED

    rebuilt = (tmp_path / "sample.py").read_text()
    # The edited function and its new comment are present.
    assert "# NEW comment written by the agent" in rebuilt
    assert "{GREETING}, {name}!" in rebuilt
    # Comments on untouched nodes survived the round trip.
    assert "# module header comment" in rebuilt
    assert "# inline import comment" in rebuilt
    assert "# a module-level constant" in rebuilt
    assert "# farewell body comment (must survive untouched)" in rebuilt
    # The old greet body comment was genuinely replaced.
    assert "greet body comment (should be replaced)" not in rebuilt
    # The result is still valid Python.
    compile(rebuilt, "sample.py", "exec")
