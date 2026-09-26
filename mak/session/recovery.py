"""Resume after a crash: resolve an interrupted commit, restore the saved wave."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

from mak.agent_runner.registry import AdapterRegistry
from mak.config import MakConfig
from mak.core.exceptions import SchedulingError
from mak.core.logging import EventType
from mak.core.types import SubTask
from mak.git_integration.git import GitHelper
from mak.node_store.store import NodeStore
from mak.node_store.transaction import recover as recover_commit
from mak.planner.depgraph import dep_graph_from_store
from mak.scheduler.scheduler import Scheduler
from mak.semantic.locking import build_lock_policy
from mak.semantic.read_set import read_set_from_json
from mak.session.concurrency import ConcurrentRunner
from mak.session.events import EventLog
from mak.session.types import LockTableLike, SubTaskProgress
from mak.session.wave import WaveState
from mak.session.workspace import Workspace


@dataclass(frozen=True, slots=True)
class RecoveredWave:
    """A wave restored from ``task_graph.json``, with the session state it saved."""

    wave: WaveState
    objective: str | None
    cascade_history: list[str]


class RecoveryManager:
    """Journal recovery and restoring a crashed wave from its persisted graph."""

    def __init__(
        self,
        *,
        config: MakConfig,
        store: NodeStore,
        workspace: Workspace,
        lock_table: LockTableLike,
        registry: AdapterRegistry,
        git: GitHelper | None,
        session_id: str,
        max_concurrent: int,
        log: EventLog,
    ) -> None:
        self._config = config
        self._store = store
        self._workspace = workspace
        self._lock_table = lock_table
        self._registry = registry
        self._git = git
        self._session_id = session_id
        self._max_concurrent = max_concurrent
        self._log = log

    def recover_journal(self) -> None:
        """Resolve a commit journal an interrupted run left behind."""
        outcome = recover_commit(
            self._workspace.journal_dir,
            self._store,
            resolve=self._workspace.safe_output_path,
            reaudit=self._reaudit,
        )
        if outcome is not None:
            self._log(EventType.SESSION_STARTED, recovered_commit=outcome)
            print(
                f"mak: recovered an interrupted commit ({outcome}).",
                file=sys.stderr,
            )

    def restore(
        self, runner: Callable[[], ConcurrentRunner]
    ) -> tuple[int, RecoveredWave | None]:
        """Expire stale leases and rebuild the saved wave, if there is one.

        Returns ``(leases expired, restored wave or None)``. A task graph that
        cannot be read yields None rather than raising, so the caller reports
        "nothing to recover" and the operator can start a fresh run: raising
        would break recovery on precisely the crash it exists to handle, since a
        kill mid-write is what truncates that file in the first place.
        """
        expired = len(self._lock_table.expire_stale())
        graph_path = self._workspace.task_graph_path
        if not graph_path.exists():
            return expired, None
        try:
            scheduler = Scheduler.from_persisted(
                graph_path,
                self._lock_table,
                runner(),
                self._registry,
                max_concurrent=self._max_concurrent,
            )
        except SchedulingError as exc:
            self._log(
                EventType.SESSION_ENDED,
                recover_failed=True,
                task_graph=str(graph_path),
                reason=str(exc),
            )
            print(
                f"mak: the saved task graph at {graph_path} could not be read "
                f"({exc}); there is nothing to resume.",
                file=sys.stderr,
            )
            return expired, None
        return expired, self._restored(scheduler)

    def _restored(self, scheduler: Scheduler) -> RecoveredWave:
        """Rebuild the wave state and the session annotations a scheduler saved."""
        objective = scheduler.annotations.get("objective")
        history = scheduler.annotations.get("cascade_history", [])
        graph = dep_graph_from_store(self._store)
        lock_policy = build_lock_policy(
            self._config.semantic,
            self._store,
            graph,
            list(scheduler.dag.tasks.values()),
        )
        scheduler.use_lock_policy(lock_policy)
        persisted = scheduler.annotations.get("read_sets", {})
        # A resumed wave inherits whatever the crashed one had already
        # written, which is the correct starting inventory for it: those
        # files do exist now, and a no-op about them is answerable.
        wave = WaveState.start(
            scheduler=scheduler,
            graph=graph,
            lock_policy=lock_policy,
            read_sets={
                str(task_id): read_set_from_json(raw)
                for task_id, raw in (
                    persisted.items() if isinstance(persisted, dict) else ()
                )
            },
            progress={
                t.task_id: _restore_progress(scheduler, t)
                for t in scheduler.dag.tasks.values()
            },
            preexisting_files={
                str(node_id).split("::", 1)[0] for node_id in self._store.list_nodes()
            },
        )
        return RecoveredWave(
            wave=wave,
            objective=objective if isinstance(objective, str) else None,
            cascade_history=(
                [str(item) for item in history] if isinstance(history, list) else []
            ),
        )

    def _reaudit(self, task_id: str, files: list[str]) -> None:
        """Re-run an audit commit whose original run was killed mid-flight."""
        if self._git is None or not self._config.git.auto_commit:
            return
        self._git.commit_task(
            task_id=task_id,
            files=files,
            description="recovered interrupted commit",
            agent_id="recovery",
            session_id=self._session_id,
        )


def _restore_progress(scheduler: Scheduler, task: SubTask) -> SubTaskProgress:
    """Grant accounting for a restored task: all done if it had completed."""
    progress = SubTaskProgress(task.task_id, list(task.target_nodes))
    if scheduler.dag.is_complete(task.task_id):
        progress.completed_nodes = set(task.target_nodes)
    return progress
